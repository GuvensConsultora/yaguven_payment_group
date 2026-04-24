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

    payment_ids = fields.One2many(
        "account.payment",
        "payment_group_id",
        string="Medios de pago",
        check_company=True,
    )
    to_pay_move_line_ids = fields.Many2many(
        "account.move.line",
        string="Comprobantes a cancelar",
        domain="[('parent_state', '=', 'posted'),"
               " ('partner_id', '=', partner_id),"
               " ('company_id', '=', company_id),"
               " ('account_id.account_type', 'in', ('asset_receivable', 'liability_payable')),"
               " ('reconciled', '=', False)]",
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
    to_pay_amount = fields.Monetary(
        compute="_compute_to_pay_amount",
        string="Total a cancelar",
        store=True,
    )
    unreconciled_amount = fields.Monetary(
        compute="_compute_unreconciled_amount",
        string="Diferencia (no conciliado)",
        store=True,
    )
    partner_open_invoices_amount = fields.Monetary(
        compute="_compute_partner_open_amounts",
        string="Facturas pendientes",
    )
    partner_open_credits_amount = fields.Monetary(
        compute="_compute_partner_open_amounts",
        string="NC sin aplicar",
    )
    partner_open_payments_amount = fields.Monetary(
        compute="_compute_partner_open_amounts",
        string="Pagos sin aplicar",
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
                ("move_id.move_type", "in", move_types),
            ])
            group.to_pay_move_line_ids = [(6, 0, lines.ids)]

    @api.depends("payment_ids.amount", "payment_ids.state")
    def _compute_payments_amount(self):
        for group in self:
            group.payments_amount = sum(
                p.amount for p in group.payment_ids if p.state != "cancel"
            )

    @api.depends("to_pay_move_line_ids", "to_pay_move_line_ids.amount_residual")
    def _compute_to_pay_amount(self):
        for group in self:
            sign = 1 if group.partner_type == "customer" else -1
            group.to_pay_amount = sign * sum(
                line.amount_residual for line in group.to_pay_move_line_ids
            )

    @api.depends("payments_amount", "to_pay_amount")
    def _compute_unreconciled_amount(self):
        for group in self:
            group.unreconciled_amount = group.payments_amount - group.to_pay_amount

    def _get_partner_open_lines(self, kind):
        """kind ∈ {'invoice', 'refund', 'payment'}. Devuelve AML abiertas del
        partner sobre la cuenta de deudores/acreedores, filtradas por tipo."""
        self.ensure_one()
        if not self.partner_id:
            return self.env["account.move.line"]
        is_customer = self.partner_type == "customer"
        account_type = "asset_receivable" if is_customer else "liability_payable"
        domain = [
            ("partner_id", "=", self.partner_id.id),
            ("company_id", "=", self.company_id.id),
            ("account_id.account_type", "=", account_type),
            ("parent_state", "=", "posted"),
            ("reconciled", "=", False),
        ]
        if kind == "invoice":
            domain.append((
                "move_id.move_type",
                "=",
                "out_invoice" if is_customer else "in_invoice",
            ))
        elif kind == "refund":
            domain.append((
                "move_id.move_type",
                "=",
                "out_refund" if is_customer else "in_refund",
            ))
        elif kind == "payment":
            # AML sobre receivable/payable cuyo move NO es factura ni NC:
            # pagos posteados sin conciliar, ajustes, recibos/OP viejos, etc.
            # No filtramos por payment_id porque es un stored related que
            # puede estar vacío en instancias migradas.
            invoice_types = (
                ("out_invoice", "out_refund")
                if is_customer
                else ("in_invoice", "in_refund")
            )
            domain.append(("move_id.move_type", "not in", invoice_types))
        return self.env["account.move.line"].search(domain)

    @api.depends("partner_id", "partner_type", "company_id")
    def _compute_partner_open_amounts(self):
        for group in self:
            group.partner_open_invoices_amount = 0.0
            group.partner_open_credits_amount = 0.0
            group.partner_open_payments_amount = 0.0
            if not group.partner_id:
                continue
            sign = 1 if group.partner_type == "customer" else -1
            invoices = group._get_partner_open_lines("invoice")
            refunds = group._get_partner_open_lines("refund")
            payments = group._get_partner_open_lines("payment")
            group.partner_open_invoices_amount = sign * sum(invoices.mapped("amount_residual"))
            group.partner_open_credits_amount = -sign * sum(refunds.mapped("amount_residual"))
            group.partner_open_payments_amount = -sign * sum(payments.mapped("amount_residual"))

    def action_open_partner_invoices(self):
        self.ensure_one()
        move_ids = self._get_partner_open_lines("invoice").mapped("move_id").ids
        return {
            "type": "ir.actions.act_window",
            "name": _("Facturas pendientes — %s") % (self.partner_id.display_name or ""),
            "res_model": "account.move",
            "view_mode": "list,form",
            "domain": [("id", "in", move_ids)],
            "context": {"create": False},
        }

    def action_open_partner_credits(self):
        self.ensure_one()
        move_ids = self._get_partner_open_lines("refund").mapped("move_id").ids
        return {
            "type": "ir.actions.act_window",
            "name": _("NC sin aplicar — %s") % (self.partner_id.display_name or ""),
            "res_model": "account.move",
            "view_mode": "list,form",
            "domain": [("id", "in", move_ids)],
            "context": {"create": False},
        }

    def action_open_partner_payments(self):
        self.ensure_one()
        move_ids = self._get_partner_open_lines("payment").mapped("move_id").ids
        return {
            "type": "ir.actions.act_window",
            "name": _("Pagos sin aplicar — %s") % (self.partner_id.display_name or ""),
            "res_model": "account.move",
            "view_mode": "list,form",
            "domain": [("id", "in", move_ids)],
            "context": {"create": False},
        }

    def _get_sequence_code(self):
        self.ensure_one()
        return (
            "account.payment.group.inbound"
            if self.payment_type == "inbound"
            else "account.payment.group.outbound"
        )

    def _get_counterpart_lines(self):
        self.ensure_one()
        return self.payment_ids.move_id.line_ids.filtered(
            lambda l: (
                l.account_id.account_type in ("asset_receivable", "liability_payable")
                and l.partner_id == self.partner_id
                and not l.reconciled
            )
        )

    def action_post(self):
        for group in self:
            if group.state != "draft":
                raise UserError(_("Sólo se pueden confirmar grupos en borrador."))
            if not group.payment_ids:
                raise UserError(_("Agregá al menos un medio de pago antes de confirmar."))

            seq_code = group._get_sequence_code()
            name = self.env["ir.sequence"].next_by_code(seq_code)
            if not name:
                raise UserError(_(
                    "No se encontró la secuencia '%s'. Verificá que el módulo esté bien cargado."
                ) % seq_code)

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
