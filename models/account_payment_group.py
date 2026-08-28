from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


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

    @api.onchange("partner_type")
    def _onchange_partner_type_sync_payment_type(self):
        """Mantiene coherencia: Cliente → Cobro / Proveedor → Pago.
        Si el usuario cambia el tipo de tercero, el tipo de movimiento
        se ajusta automáticamente."""
        for group in self:
            if group.partner_type == "supplier":
                group.payment_type = "outbound"
            elif group.partner_type == "customer":
                group.payment_type = "inbound"

    @api.constrains("partner_type", "payment_type")
    def _check_partner_payment_type_coherence(self):
        """Bloquea guardar combinaciones incoherentes: un proveedor solo
        puede tener una OP de Pago, un cliente solo un Recibo de Cobro.
        Cubre creates por XML-RPC, importaciones y duplicates donde el
        onchange de UI no se dispara."""
        for group in self:
            if (group.partner_type == "supplier"
                    and group.payment_type != "outbound"):
                raise ValidationError(_(
                    "Una OP a proveedor debe ser de tipo 'Pago' "
                    "(outbound). Recibido: %s."
                ) % group.payment_type)
            if (group.partner_type == "customer"
                    and group.payment_type != "inbound"):
                raise ValidationError(_(
                    "Un Recibo de cliente debe ser de tipo 'Cobro' "
                    "(inbound). Recibido: %s."
                ) % group.payment_type)

    @api.onchange("partner_id", "partner_type", "company_id")
    def _onchange_partner_autofill_to_pay(self):
        for group in self:
            if group.state != "draft" or not group.partner_id:
                group.to_pay_move_line_ids = [(5, 0, 0)]
                continue
            lines = group._get_pending_move_lines()
            group.to_pay_move_line_ids = [(6, 0, lines.ids)]

    def _get_pending_move_lines(self):
        """Devuelve las líneas pendientes (residual != 0) del partner que
        corresponden a su tipo (receivable cliente / payable proveedor)."""
        self.ensure_one()
        if not self.partner_id:
            return self.env["account.move.line"]
        account_type = (
            "asset_receivable"
            if self.partner_type == "customer"
            else "liability_payable"
        )
        move_types = (
            ("out_invoice", "out_refund")
            if self.partner_type == "customer"
            else ("in_invoice", "in_refund")
        )
        return self.env["account.move.line"].search([
            ("partner_id", "=", self.partner_id.id),
            ("company_id", "=", self.company_id.id),
            ("account_id.account_type", "=", account_type),
            ("parent_state", "=", "posted"),
            ("reconciled", "=", False),
            ("amount_residual", "!=", 0),
            ("move_id.move_type", "in", move_types),
        ])

    def action_load_all_pending(self):
        """Botón: imputa todos los comprobantes pendientes del partner."""
        for group in self:
            if group.state != "draft":
                raise UserError(_(
                    "Solo se pueden cargar comprobantes en estado borrador."
                ))
            lines = group._get_pending_move_lines()
            group.to_pay_move_line_ids = [(6, 0, lines.ids)]
        return True

    def action_clear_to_pay(self):
        """Botón: vacía la lista de comprobantes a cancelar."""
        for group in self:
            if group.state != "draft":
                raise UserError(_(
                    "Solo se puede limpiar la lista en estado borrador."
                ))
            group.to_pay_move_line_ids = [(5, 0, 0)]
        return True

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
            residual = abs(line.amount_residual)
            if line.move_id.move_type in ("in_refund", "out_refund"):
                return -residual
            return residual
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
        amount = sum(partials.mapped("amount"))
        # NC/ND invierten el signo: son descuentos en el total a cancelar
        if line.move_id.move_type in ("in_refund", "out_refund"):
            return -amount
        return amount

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
                    # _get_line_cancelled_amount ya devuelve la NC/ND con signo
                    # invertido (líneas ~271-272) — no volver a invertirla acá.
                    total += group._get_line_cancelled_amount(l)
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
        de retención embebidas (patrón ADHOC adaptado, no Odoo nativo).

        Diseño: una línea por retención con `tax_line_id` y
        `tax_base_amount` setados. NO genera líneas dummy en la cuenta
        base de retención — la base se persiste como atributo de la
        línea (`tax_base_amount`) y como atributo del registro
        `account.payment.group.withholding`. El motor de impuestos de
        Odoo encuentra ambos datos sin necesidad del par de líneas
        compensatorias en la cuenta puente.

        Por qué este patrón y no el del wizard nativo Odoo 19:
        ADHOC (referencia de mercado en AR) nunca usó el patrón de base
        lines. El nativo lo agrega sólo para que reportes que iteran
        `tax_ids` encuentren la base. En Camilleti decidimos no usar
        ADHOC y nuestros propios reportes (acumulado RG 830, SICORE,
        certificado, listado del período) leen base desde
        `tax_line_id` + `tax_base_amount` directamente, así que las
        dummy lines no aportan nada y son ruido visual.

        Elige como target el payment de mayor importe (típicamente la
        pata de efectivo/transferencia) para no tocar moves de cheques
        que tienen su propia estructura. El usuario debe haber cargado
        en el target un `amount` igual al neto (facturas − retenciones);
        `_prepare_move_lines_per_type` ajusta el counterpart
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

        # psign: +1 cobro (inbound), -1 pago (outbound).
        psign = 1 if target.payment_type == "inbound" else -1
        self.withholding_ids._ensure_name()

        net = target.amount
        wth_total = sum(self.withholding_ids.mapped("amount"))
        gross = net + wth_total
        cur = target.currency_id.id

        # Asiento EXPLÍCITO (line_ids) — control total para que la
        # contrapartida quede en BRUTO y cancele la factura, y la línea
        # de retención conserve base + régimen para el SICORE.
        # No usamos write_off_line_vals: el motor nativo de withholding
        # de Odoo 19 los descarta al combinarlos en _prepare_move_lines_per_type
        # ("We don't support to combine write_off_lines and withholding_lines"),
        # dejando la contrapartida en el NETO y la factura sin cancelar.
        line_ids = [
            (0, 0, {
                "name": target.payment_method_line_id.name or _("Pago"),
                "account_id": target.outstanding_account_id.id,
                "balance": psign * net,
                "amount_currency": psign * net,
                "currency_id": cur,
            }),
            (0, 0, {
                "name": self.communication or self.name or _("Recibo/OP"),
                "account_id": target.destination_account_id.id,
                "partner_id": self.partner_id.id,
                "balance": -psign * gross,
                "amount_currency": -psign * gross,
                "currency_id": cur,
            }),
        ]
        for w in self.withholding_ids:
            # _tax_compute_all_helper aplica RG 830 (mínimo no sujeto,
            # escala, mínimo de retención, acumulado del mes) y devuelve
            # la cuenta de retención + repartition. El importe imputado es
            # w.amount (en migración = el valor histórico ya 830; en alta
            # nueva el usuario carga el que el helper calcula).
            _expected, account_id, repartition_id = w._tax_compute_all_helper()
            line_ids.append((0, 0, {
                "name": w.name,
                "account_id": account_id,
                "balance": psign * w.amount,
                "amount_currency": psign * w.amount,
                "tax_base_amount": psign * w.base_amount,
                "tax_repartition_line_id": repartition_id,
                "currency_id": cur,
            }))

        target._generate_journal_entry(line_ids=line_ids)

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

            # La fecha del recibo manda sobre la de sus pagos. Un recibo se
            # puede armar un día y confirmar otro —queda en borrador porque no
            # se cobró—, y hasta acá el pago conservaba la fecha con que se
            # creó: el recibo impreso decía una fecha y el asiento otra.
            # Caso 2026-08-27, MADERAS SSAP: recibo del 27, pago del 22.
            if group.payment_date:
                desfasados = group.payment_ids.filtered(
                    lambda p: p.date != group.payment_date)
                for pay in desfasados:
                    pay.date = group.payment_date
                    if pay.move_id and pay.move_id.state == "draft":
                        pay.move_id.date = group.payment_date

            group._check_anticipo_balance()
            group._apply_withholdings_to_target_payment()
            group.withholding_ids._post_calculation_check_to_chatter()

            # En Odoo 19 account.payment.action_post() ya no crea el
            # move; lo difiere al matching con statement bancaria. Para
            # que el recibo/OP impacte en contabilidad al confirmar,
            # forzamos _generate_journal_entry en cada payment del
            # grupo que no tenga move (los target con retención ya lo
            # tienen porque _apply_withholdings_to_target_payment los
            # generó arriba). Después posteamos todos los moves draft —
            # las base lines de retención necesitan parent_state='posted'
            # para que el acumulado RG 830 las vea.
            for pay in group.payment_ids.filtered(lambda p: not p.move_id):
                pay._generate_journal_entry()

            for pay in group.payment_ids.filtered(
                lambda p: p.move_id and p.move_id.state == "draft"
            ):
                pay.move_id.action_post()

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
                # Desconciliar las líneas matcheadas (payment ↔ factura)
                # para que la factura vuelva a quedar pendiente.
                if group.matched_move_line_ids:
                    group.matched_move_line_ids.remove_move_reconcile()

                # PCGA / RT FACPCE: un asiento posteado no se elimina ni
                # se pone en estado "cancelado" — se mantiene y se
                # registra un asiento de reversión con D ↔ C invertidos,
                # fecha actual, que lo neutraliza contablemente. Esto
                # preserva la integridad cronológica y la auditoría.
                #
                # _reverse_moves(cancel=True) crea el reverso, lo postea
                # y lo concilia con el original. El move original queda
                # intacto en estado 'posted'.
                to_cancel = group.payment_ids.filtered(
                    lambda p: p.state not in ("canceled", "rejected")
                )
                if to_cancel:
                    posted_moves = to_cancel.move_id.filtered(
                        lambda mv: mv.state == "posted"
                    )
                    draft_moves = to_cancel.move_id.filtered(
                        lambda mv: mv.state == "draft"
                    )
                    draft_moves.unlink()
                    if posted_moves:
                        today = fields.Date.context_today(self)
                        posted_moves._reverse_moves(
                            default_values_list=[
                                {"date": today, "ref": _("Reversión de %s") % mv.name}
                                for mv in posted_moves
                            ],
                            cancel=True,
                        )
                    to_cancel.write({"state": "canceled"})
            group.write({
                "state": "cancel",
                "matched_move_line_ids": [(5, 0, 0)],
            })
        return True

    def action_draft(self):
        # PCGA / RT FACPCE: una vez cancelado, el comprobante generó un
        # asiento de reversión que mantiene la integridad cronológica.
        # Volver a borrador implicaría borrar el reverso y desreversar
        # el original, rompiendo el principio de inalterabilidad. Si
        # hace falta reemitir el cobro/pago, crear un comprobante nuevo.
        raise UserError(_(
            "Un comprobante cancelado no vuelve a borrador. La cancelación "
            "generó un asiento de reversión que mantiene la trazabilidad "
            "contable (PCGA / RT FACPCE). Para reemitir el cobro/pago, "
            "creá un comprobante nuevo."
        ))
