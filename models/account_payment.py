from odoo import _, fields, models
from odoo.exceptions import UserError


class AccountPayment(models.Model):
    _inherit = "account.payment"

    payment_group_id = fields.Many2one(
        "account.payment.group",
        string="Recibo / OP",
        ondelete="cascade",
        index=True,
        copy=False,
    )

    is_card_journal = fields.Boolean(
        related="journal_id.is_card_journal",
        store=False,
    )
    card_brand = fields.Selection(
        [
            ("visa", "Visa"),
            ("master", "Mastercard"),
            ("amex", "American Express"),
            ("cabal", "Cabal"),
            ("naranja", "Naranja"),
            ("maestro", "Maestro"),
            ("other", "Otra"),
        ],
        string="Marca",
    )
    card_last_digits = fields.Char(
        string="Últimos 4 dígitos",
        size=4,
    )
    card_voucher_number = fields.Char(string="Cupón")
    card_authorization_code = fields.Char(string="Cód. autorización")
    card_installments = fields.Integer(string="Cuotas", default=1)
    card_holder_name = fields.Char(string="Titular")

    def action_open_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_(
                "Este medio de pago todavía no tiene asiento contable. "
                "Confirmá el recibo/OP para generarlo."
            ))
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": self.move_id.id,
            "view_mode": "form",
            "target": "current",
        }
