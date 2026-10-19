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

    # ── Sincronización pago ↔ asiento con retenciones ────────────────────
    #
    # Cuando el recibo/OP tiene retenciones, el asiento NO es el de dos líneas
    # que arma Odoo: `_apply_withholdings` lo genera explícitamente con la
    # contrapartida en BRUTO más una línea por retención (ver
    # account_payment_group.py). El sincronizador nativo no conoce esas líneas:
    # al leer o regenerar el asiento lo reconstruye con la contrapartida en
    # NETO, y la diferencia es exactamente el importe retenido.
    #
    # Efecto práctico del bug (Arauco, 06/08/2026): `button_draft` sobre
    # cualquier pago con retenciones fallaba con "El asiento no está
    # balanceado" AUNQUE el asiento estuviera balanceado (débito − crédito = 0).
    # Como draft → write → post es el mecanismo con el que se corrige todo,
    # esos pagos quedaban imposibles de editar. Reproducción: account.payment
    # 1984 (PMACR/2026/00008), con $15.633,63 de retenciones sobre $442.500.
    #
    # Estos pagos quedan fuera de la sincronización en las dos direcciones: su
    # asiento es responsabilidad de este módulo, que lo arma completo al
    # confirmar el grupo.

    def _con_retenciones(self):
        """Pagos cuyo asiento arma este módulo por tener retenciones."""
        return self.filtered(lambda p: p.payment_group_id.withholding_ids)

    def _synchronize_to_moves(self, changed_fields):
        return super(
            AccountPayment, self - self._con_retenciones()
        )._synchronize_to_moves(changed_fields)

    def _synchronize_from_moves(self, changed_fields):
        return super(
            AccountPayment, self - self._con_retenciones()
        )._synchronize_from_moves(changed_fields)

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

    # Transferencia: el Nº de operación va en el `payment_reference` nativo
    # (Odoo lo usa para sugerir el apareo con el extracto). Sin él, una
    # transferencia que el cliente dijo haber hecho y nunca entró al banco
    # no se distingue de una que sí entró.
    is_transfer_journal = fields.Boolean(compute="_compute_is_transfer_journal")

    @api.depends("journal_id.type", "journal_id.is_card_journal")
    def _compute_is_transfer_journal(self):
        for pay in self:
            pay.is_transfer_journal = (
                pay.journal_id.type == "bank" and not pay.journal_id.is_card_journal
            )

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
