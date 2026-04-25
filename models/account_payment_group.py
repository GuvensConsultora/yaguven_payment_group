from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountPaymentGroup(models.Model):
    _name = "account.payment.group"
    _description = "Recibo / Orden de pago — agrupa varios account.payment contra varias facturas"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "payment_date desc, id desc"
    _check_company_auto = True

    name = fields.Char(
        string="Número",
        copy=False,
        readonly=True,
        default=lambda self: _("Borrador"),
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(
        related="company_id.currency_id",
        store=True,
    )
    partner_type = fields.Selection(
        [("customer", "Cliente"), ("supplier", "Proveedor")],
        required=True,
        default="customer",
    )
    payment_type = fields.Selection(
        [("inbound", "Cobro"), ("outbound", "Pago")],
        required=True,
        default="inbound",
    )
    partner_id = fields.Many2one(
        "res.partner",
        required=True,
        check_company=True,
    )
    payment_date = fields.Date(
        required=True,
        default=fields.Date.context_today,
    )
    communication = fields.Char(string="Concepto")
    state = fields.Selection(
        [("draft", "Borrador"), ("posted", "Confirmado"), ("cancel", "Cancelado")],
        required=True,
        default="draft",
        tracking=True,
    )
    book_id = fields.Many2one(
        "account.payment.group.book",
        string="Talonario",
        domain="[('partner_type', '=', partner_type),"
               " ('payment_type', '=', payment_type),"
               " '|', ('company_id', '=', company_id), ('company_id', '=', False),"
               " ('active', '=', True)]",
        tracking=True,
    )

    payment_ids = fields.One2many(
        "account.payment",
        "payment_group_id",
        string="Medios de pago",
        check_company=True,
    )
    withholding_ids = fields.One2many(
        "account.payment.group.withholding",
        "payment_group_id",
        string="Retenciones",
    )
    to_pay_move_line_ids = fields.Many2many(
        "account.move.line",
        string="Comprobantes a cancelar",
        domain="[('parent_state', '=', 'posted'),"
               " ('partner_id', '=', partner_id),"
               " ('company_id', '=', company_id),"
               " ('account_id.account_type', 'in', ('asset_receivable', 'liability_payable')),"
               " ('reconciled', '=', False),"
               " ('amount_residual', '!=', 0)]",
    )
    matched_move_line_ids = fields.Many2many(
        "account.move.line",
        "account_payment_group_matched_line_rel",
        string="Líneas conciliadas",
        copy=False,
        readonly=True,
    )

    payments_amount = fields.Monetary(
        compute="_compute_payments_amount",
        string="Total medios de pago",
        store=True,
    )
    withholdings_amount = fields.Monetary(
        compute="_compute_withholdings_amount",
        string="Total retenciones",
        store=True,
    )
    to_pay_amount = fields.Monetary(
        compute="_compute_to_pay_amount",
        string="Total a cancelar (neto)",
        store=True,
    )
    invoices_to_cancel_amount = fields.Monetary(
        compute="_compute_invoices_to_cancel_amount",
        string="Facturas a cancelar",
        store=True,
    )
    net_to_cancel_amount = fields.Monetary(
        compute="_compute_net_to_cancel_amount",
        string="Neto a cancelar",
        store=True,
        help="Facturas a cancelar menos medios de pago. "
             "Positivo = queda deuda; negativo = sobrante.",
    )
    advance_amount = fields.Monetary(
        compute="_compute_advance_amount",
        store=True,
        readonly=False,
        string="Anticipo sin factura",
        help="Si no hay facturas tildadas, toma automáticamente el total "
             "de medios de pago. Es la base desde la que se calculan las "
             "retenciones que se carguen como medios de pago. Editable "
             "manualmente cuando no hay facturas.",
    )
    partner_balance_amount = fields.Monetary(
        compute="_compute_partner_balance_amount",
        string="Saldo del tercero",
    )
    pending_balance_amount = fields.Monetary(
        compute="_compute_pending_balance_amount",
        string="Saldo pendiente",
        store=True,
        help="Suma de residuales no conciliados de las facturas cubiertas "
             "por este recibo/OP. Si los medios de pago no alcanzaron a "
             "cubrir todos los comprobantes, este monto refleja el saldo "
             "que el tercero sigue debiendo por esas facturas.",
    )

    @api.onchange("partner_id", "partner_type", "company_id")
    def _onchange_partner_autofill_to_pay(self):
        for group in self:
            if group.state != "draft" or not group.partner_id:
                group.to_pay_move_line_ids = [(5, 0, 0)]
                continue
            account_type = (
                "asset_receivable"
                if group.partner_type == "customer"
                else "liability_payable"
            )
            move_types = (
                ("out_invoice", "out_refund")
                if group.partner_type == "customer"
                else ("in_invoice", "in_refund")
            )
            lines = self.env["account.move.line"].search([
                ("partner_id", "=", group.partner_id.id),
                ("company_id", "=", group.company_id.id),
                ("account_id.account_type", "=", account_type),
                ("parent_state", "=", "posted"),
                ("reconciled", "=", False),
                ("amount_residual", "!=", 0),
                ("move_id.move_type", "in", move_types),
            ])
            group.to_pay_move_line_ids = [(6, 0, lines.ids)]

    @api.depends("payment_ids.amount", "payment_ids.state")
    def _compute_payments_amount(self):
        for group in self:
            group.payments_amount = sum(
                p.amount for p in group.payment_ids if p.state != "cancel"
            )

    @api.depends("withholding_ids.amount")
    def _compute_withholdings_amount(self):
        for group in self:
            group.withholdings_amount = sum(group.withholding_ids.mapped("amount"))

    def _get_line_cancelled_amount(self, line):
        """Importe cancelado de `line` en este grupo.

        En draft devuelve |residual| (estimación de lo que se va a cancelar
        si los payments alcanzan). En posted deriva del reconciliation real:
        suma los parciales cuya contraparte esté en `matched_move_line_ids`
        del grupo, para que el importe se mantenga correcto aunque el
        residual del AML ya haya quedado en 0.
        """
        self.ensure_one()
        if self.state != "posted":
            return abs(line.amount_residual)
        counterparts = self.matched_move_line_ids - line
        if not counterparts:
            return 0.0
        # Convención Odoo: matched_debit_ids es inverso de
        # partial_reconcile.credit_move_id (self es el credit, counterpart
        # es debit_move_id); matched_credit_ids es inverso de debit_move_id
        # (self es el debit, counterpart es credit_move_id).
        partials = line.matched_debit_ids.filtered(
            lambda p: p.debit_move_id in counterparts
        ) | line.matched_credit_ids.filtered(
            lambda p: p.credit_move_id in counterparts
        )
        return sum(partials.mapped("amount"))

    @api.depends(
        "state",
        "to_pay_move_line_ids",
        "to_pay_move_line_ids.amount_residual",
        "matched_move_line_ids",
        "matched_move_line_ids.amount_residual",
    )
    def _compute_to_pay_amount(self):
        for group in self:
            if group.state == "posted":
                group.to_pay_amount = sum(
                    group._get_line_cancelled_amount(l)
                    for l in group.to_pay_move_line_ids
                )
                continue
            sign = 1 if group.partner_type == "customer" else -1
            group.to_pay_amount = sign * sum(
                line.amount_residual for line in group.to_pay_move_line_ids
            )

    @api.depends(
        "state",
        "to_pay_move_line_ids",
        "to_pay_move_line_ids.amount_residual",
        "to_pay_move_line_ids.move_id.move_type",
        "matched_move_line_ids",
        "matched_move_line_ids.amount_residual",
        "partner_type",
    )
    def _compute_invoices_to_cancel_amount(self):
        for group in self:
            if group.partner_type == "customer":
                doc_types = ("out_invoice", "out_refund")
                refund_type = "out_refund"
                sign = 1
            else:
                doc_types = ("in_invoice", "in_refund")
                refund_type = "in_refund"
                sign = -1
            doc_lines = group.to_pay_move_line_ids.filtered(
                lambda l: l.move_id.move_type in doc_types
            )
            if group.state == "posted":
                total = 0.0
                for l in doc_lines:
                    amt = group._get_line_cancelled_amount(l)
                    if l.move_id.move_type == refund_type:
                        amt = -amt
                    total += amt
                group.invoices_to_cancel_amount = total
                continue
            group.invoices_to_cancel_amount = sign * sum(
                doc_lines.mapped("amount_residual")
            )

    @api.depends("invoices_to_cancel_amount", "payments_amount", "withholdings_amount")
    def _compute_net_to_cancel_amount(self):
        for group in self:
            group.net_to_cancel_amount = (
                group.invoices_to_cancel_amount
                - group.payments_amount
                - group.withholdings_amount
            )

    @api.depends(
        "state",
        "to_pay_move_line_ids",
        "to_pay_move_line_ids.amount_residual",
        "matched_move_line_ids",
    )
    def _compute_pending_balance_amount(self):
        for group in self:
            if group.state != "posted":
                group.pending_balance_amount = 0.0
                continue
            group.pending_balance_amount = sum(
                abs(l.amount_residual) for l in group.to_pay_move_line_ids
            )

    @api.depends(
        "payments_amount", "invoices_to_cancel_amount", "state",
        "withholding_ids",
    )
    def _compute_advance_amount(self):
        """Auto-completa el bruto del anticipo en el caso simple
        (sin facturas, sin retenciones): advance = payments_amount.

        En cualquier otro caso (hay facturas, o hay retenciones, o
        ambos), el usuario controla el campo manualmente: ese es el
        flujo correcto para mezclar cancelación de facturas con un
        anticipo en el mismo comprobante (RG 830 lo prevé), y para
        anticipos con retención donde el bruto ≠ neto cash.
        """
        for group in self:
            if group.state != "draft":
                continue
            if group.invoices_to_cancel_amount or group.withholding_ids:
                continue
            group.advance_amount = group.payments_amount

    @api.onchange("partner_type", "payment_type", "company_id")
    def _onchange_autoselect_book(self):
        for group in self:
            if group.book_id and (
                group.book_id.partner_type != group.partner_type
                or group.book_id.payment_type != group.payment_type
                or (
                    group.book_id.company_id
                    and group.book_id.company_id != group.company_id
                )
            ):
                group.book_id = False
            if not group.book_id and group.partner_type and group.payment_type:
                group.book_id = self.env["account.payment.group.book"].search([
                    ("partner_type", "=", group.partner_type),
                    ("payment_type", "=", group.payment_type),
                    "|",
                    ("company_id", "=", group.company_id.id),
                    ("company_id", "=", False),
                    ("active", "=", True),
                ], limit=1)

    def _get_partner_open_account_domain(self):
        """Domain sobre account.move.line de pendientes del tercero en su
        cuenta de deudores/acreedores."""
        self.ensure_one()
        is_customer = self.partner_type == "customer"
        account_type = "asset_receivable" if is_customer else "liability_payable"
        return [
            ("partner_id", "=", self.partner_id.id),
            ("company_id", "=", self.company_id.id),
            ("account_id.account_type", "=", account_type),
            ("parent_state", "=", "posted"),
            ("reconciled", "=", False),
            ("amount_residual", "!=", 0),
        ]

    @api.depends("partner_id", "partner_type", "company_id")
    def _compute_partner_balance_amount(self):
        for group in self:
            group.partner_balance_amount = 0.0
            if not group.partner_id:
                continue
            sign = 1 if group.partner_type == "customer" else -1
            lines = self.env["account.move.line"].search(
                group._get_partner_open_account_domain()
            )
            group.partner_balance_amount = sign * sum(lines.mapped("amount_residual"))

    def action_print_recibo(self):
        self.ensure_one()
        return self.env.ref(
            "yaguven_payment_group.action_report_payment_group"
        ).report_action(self)

    def action_open_partner_account(self):
        self.ensure_one()
        if not self.partner_id:
            return False
        return {
            "type": "ir.actions.act_window",
            "name": _("Estado de cuenta — %s") % self.partner_id.display_name,
            "res_model": "account.move.line",
            "view_mode": "list,form",
            "domain": self._get_partner_open_account_domain(),
            "context": {"create": False, "search_default_group_by_move": 1},
        }

    def _get_next_sequence_number(self):
        """Devuelve el próximo número del talonario, o del fallback por
        tipo si el recibo no tiene talonario asignado (compatibilidad)."""
        self.ensure_one()
        if self.book_id:
            return self.book_id.sequence_id.next_by_id()
        fallback_code = (
            "account.payment.group.inbound"
            if self.payment_type == "inbound"
            else "account.payment.group.outbound"
        )
        return self.env["ir.sequence"].next_by_code(fallback_code)

    def _get_counterpart_lines(self):
        self.ensure_one()
        return self.payment_ids.move_id.line_ids.filtered(
            lambda l: (
                l.account_id.account_type in ("asset_receivable", "liability_payable")
                and l.partner_id == self.partner_id
                and not l.reconciled
            )
        )

    def _check_anticipo_balance(self):
        """Si hay retenciones, validar que lo que se cancela cuadre con
        lo que se entrega al partner.

        Bruto cancelado = invoices_to_cancel_amount + advance_amount
        Neto entregado  = payments_amount + withholdings_amount

        RG 830 calcula la retención sobre el bruto. Si los dos lados no
        coinciden, o la base de la retención no refleja la obligación
        real, o el usuario olvidó ajustar el anticipo o los medios de
        pago. Cubre los tres casos: sólo facturas con retención, sólo
        anticipo con retención, mixto facturas + anticipo + retención.
        """
        self.ensure_one()
        if not self.withholding_ids:
            return
        bruto = self.invoices_to_cancel_amount + self.advance_amount
        neto = self.payments_amount + self.withholdings_amount
        diff = bruto - neto
        if not self.currency_id.is_zero(diff):
            raise UserError(_(
                "Comprobante descuadrado.\n"
                "Bruto a cancelar (facturas + anticipo) : %(bruto)s\n"
                "  - Facturas a cancelar               : %(inv)s\n"
                "  - Anticipo sin factura              : %(adv)s\n"
                "Entregado al partner (cash + retenc.)  : %(neto)s\n"
                "  - Medios de pago (neto)             : %(pay)s\n"
                "  - Retenciones                       : %(wth)s\n"
                "Diferencia                            : %(diff)s\n\n"
                "El bruto debe igualar al neto. Ajustá el anticipo o los "
                "medios de pago para que cuadre — RG 830 calcula la "
                "retención sobre el bruto, no sobre el cash neto.",
                bruto=bruto, inv=self.invoices_to_cancel_amount,
                adv=self.advance_amount, neto=neto,
                pay=self.payments_amount, wth=self.withholdings_amount,
                diff=diff,
            ))

    def _apply_withholdings_to_target_payment(self):
        """Genera el asiento del payment target con las write-off lines
        de retención embebidas, usando el mismo flujo que el wizard nativo
        (`_generate_journal_entry(write_off_line_vals=...)`).

        Elige como target el payment de mayor importe (típicamente la pata
        de efectivo/transferencia) para no tocar moves de cheques que
        tienen su propia estructura. El usuario debe haber cargado en el
        target un `amount` igual al neto (facturas − retenciones); el
        método nativo `_prepare_move_lines_per_type` ajusta el counterpart
        automáticamente para que el move quede balanceado.
        """
        self.ensure_one()
        if not self.withholding_ids:
            return
        if not self.payment_ids:
            raise UserError(_(
                "Las retenciones requieren al menos un medio de pago donde "
                "imputar el asiento contable."
            ))

        target = self.payment_ids.sorted(
            key=lambda p: p.amount, reverse=True
        )[0]
        if target.move_id:
            raise UserError(_(
                "El medio de pago donde se imputan las retenciones ya "
                "tiene asiento generado. Eliminalo y volvelo a cargar "
                "antes de confirmar el recibo/OP."
            ))
        if not target.outstanding_account_id:
            raise UserError(_(
                "El método de pago debe tener cuenta outstanding "
                "configurada para poder aplicar retenciones (ver "
                "account.payment.method.line.payment_account_id)."
            ))

        base_account = self.company_id.l10n_ar_tax_base_account_id
        if not base_account:
            raise UserError(_(
                "Configurá la cuenta base de retención en la compañía "
                "(Configuración → Contabilidad → Cuenta base retención AR)."
            ))

        sign = 1 if self.partner_type == "customer" else -1
        self.withholding_ids._ensure_name()

        write_off_line_vals = []
        for w in self.withholding_ids:
            amount, account_id, repartition_id = w._tax_compute_all_helper()
            write_off_line_vals.append({
                "name": w.name,
                "account_id": account_id,
                "amount_currency": sign * amount,
                "balance": sign * amount,
                "tax_base_amount": sign * w.base_amount,
                "tax_repartition_line_id": repartition_id,
                "currency_id": target.currency_id.id,
            })

        for base in set(self.withholding_ids.mapped("base_amount")):
            wlines = self.withholding_ids.filtered(
                lambda x: x.base_amount == base
            )
            label = ", ".join(wlines.mapped("name"))
            signed = sign * base
            write_off_line_vals.append({
                "name": label,
                "tax_ids": [(6, 0, wlines.mapped("tax_id").ids)],
                "account_id": base_account.id,
                "balance": signed,
                "amount_currency": signed,
                "currency_id": target.currency_id.id,
            })
            write_off_line_vals.append({
                "name": label,
                "account_id": base_account.id,
                "balance": -signed,
                "amount_currency": -signed,
                "currency_id": target.currency_id.id,
            })

        target._generate_journal_entry(
            write_off_line_vals=write_off_line_vals
        )

    def action_post(self):
        for group in self:
            if group.state != "draft":
                raise UserError(_("Sólo se pueden confirmar grupos en borrador."))
            if not group.payment_ids:
                raise UserError(_("Agregá al menos un medio de pago antes de confirmar."))

            name = group._get_next_sequence_number()
            if not name:
                raise UserError(_(
                    "No se pudo obtener el próximo número. "
                    "Verificá que el talonario tenga una secuencia asignada."
                ))

            group._check_anticipo_balance()
            group._apply_withholdings_to_target_payment()

            draft_payments = group.payment_ids.filtered(lambda p: p.state == "draft")
            if draft_payments:
                draft_payments.action_post()

            counterpart = group._get_counterpart_lines()
            to_reconcile = counterpart | group.to_pay_move_line_ids
            matched = self.env["account.move.line"]
            if to_reconcile:
                accounts = to_reconcile.mapped("account_id")
                if len(accounts) > 1:
                    raise UserError(_(
                        "Los medios de pago y las facturas seleccionadas deben usar "
                        "la misma cuenta contable de deudores/acreedores. "
                        "Cuentas encontradas: %s.\n\n"
                        "Los diarios con cuenta 'outstanding' separada no están "
                        "soportados en esta versión. Configurá el diario de pago "
                        "para que impacte directamente la cuenta del cliente/proveedor."
                    ) % ", ".join(accounts.mapped("code")))
                to_reconcile.reconcile()
                matched = to_reconcile

            group.write({
                "name": name,
                "state": "posted",
                "matched_move_line_ids": [(6, 0, matched.ids)],
            })
        return True

    def action_cancel(self):
        for group in self:
            if group.state == "cancel":
                continue
            if group.state == "posted":
                if group.matched_move_line_ids:
                    group.matched_move_line_ids.remove_move_reconcile()
                posted = group.payment_ids.filtered(lambda p: p.state == "posted")
                if posted:
                    posted.action_cancel()
            group.write({
                "state": "cancel",
                "matched_move_line_ids": [(5, 0, 0)],
            })
        return True

    def action_draft(self):
        for group in self:
            if group.state != "cancel":
                raise UserError(_("Solo se puede volver a borrador desde el estado cancelado."))
            cancelled = group.payment_ids.filtered(lambda p: p.state == "cancel")
            if cancelled:
                cancelled.action_draft()
            group.state = "draft"
        return True
