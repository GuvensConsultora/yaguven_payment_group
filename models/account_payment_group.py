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
    to_pay_amount = fields.Monetary(
        compute="_compute_to_pay_amount",
        string="Total a cancelar (neto)",
        store=True,
    )
    invoices_to_cancel_amount = fields.Monetary(
        compute="_compute_invoices_to_cancel_amount",
        string="Total facturas a cancelar",
        store=True,
    )
    advance_amount = fields.Monetary(
        string="Anticipo sin factura",
        help="Importe del anticipo a registrar cuando NO hay facturas "
             "tildadas. Es la base desde la que se calculan las "
             "retenciones que se carguen como medios de pago.",
    )
    partner_balance_amount = fields.Monetary(
        compute="_compute_partner_balance_amount",
        string="Saldo del tercero",
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

    @api.depends("to_pay_move_line_ids", "to_pay_move_line_ids.amount_residual")
    def _compute_to_pay_amount(self):
        for group in self:
            sign = 1 if group.partner_type == "customer" else -1
            group.to_pay_amount = sign * sum(
                line.amount_residual for line in group.to_pay_move_line_ids
            )

    @api.depends(
        "to_pay_move_line_ids",
        "to_pay_move_line_ids.amount_residual",
        "to_pay_move_line_ids.move_id.move_type",
        "partner_type",
    )
    def _compute_invoices_to_cancel_amount(self):
        for group in self:
            sign = 1 if group.partner_type == "customer" else -1
            invoice_types = (
                ("out_invoice",)
                if group.partner_type == "customer"
                else ("in_invoice",)
            )
            invoice_lines = group.to_pay_move_line_ids.filtered(
                lambda l: l.move_id.move_type in invoice_types
            )
            group.invoices_to_cancel_amount = sign * sum(
                invoice_lines.mapped("amount_residual")
            )

    @api.onchange("to_pay_move_line_ids")
    def _onchange_reset_advance(self):
        for group in self:
            if group.invoices_to_cancel_amount:
                group.advance_amount = 0.0

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
