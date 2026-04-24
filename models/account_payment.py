from odoo import fields, models


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
