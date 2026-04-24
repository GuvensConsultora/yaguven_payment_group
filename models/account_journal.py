from odoo import fields, models


class AccountJournal(models.Model):
    _inherit = "account.journal"

    is_card_journal = fields.Boolean(
        string="Diario de tarjeta",
        help="Tildar cuando este diario corresponde a tarjetas (crédito o "
             "débito). En el form de medio de pago aparecerán los campos "
             "específicos: cupón, últimos 4 dígitos, marca, cód. de "
             "autorización, cuotas y titular.",
    )
