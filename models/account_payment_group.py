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

    def action_post(self):
        raise UserError(_("La confirmación del grupo todavía no está implementada."))

    def action_cancel(self):
        raise UserError(_("La cancelación del grupo todavía no está implementada."))

    def action_draft(self):
        raise UserError(_("El pase a borrador del grupo todavía no está implementado."))
