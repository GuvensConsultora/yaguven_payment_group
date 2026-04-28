from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountPayment(models.Model):
    _inherit = "account.payment"

    @api.depends("payment_method_line_id", "journal_id")
    def _compute_outstanding_account_id(self):
        """Si la method line no tiene payment_account configurado,
        caer al default_account del journal. En Odoo 19 nativo el
        payment queda con outstanding=False y el move se difiere al
        matching bancario; para PyME AR (caja chica + bancos sin
        cuentas puente) preferimos asiento directo banco→deudor al
        confirmar el recibo/OP. Sin este fallback,
        _generate_journal_entry filtra el payment y nunca crea el
        asiento, dejando el recibo confirmado pero sin contabilidad.
        """
        super()._compute_outstanding_account_id()
        for pay in self:
            if not pay.outstanding_account_id and pay.journal_id.default_account_id:
                pay.outstanding_account_id = pay.journal_id.default_account_id

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
