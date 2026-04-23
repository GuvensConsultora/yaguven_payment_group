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
