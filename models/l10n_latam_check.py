from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class L10nLatamCheck(models.Model):
    _inherit = "l10n_latam.check"

    is_echeq = fields.Boolean(
        string="Echeq",
        help="Tildar si es cheque electrónico (ECHEQ). Cambia el plazo "
             "máximo entre emisión y pago a 360 días (vs 30 del cheque "
             "común).",
    )
    endorser_vat = fields.Char(
        string="CUIT endosante",
        help="CUIT del último endosante del cheque. Para cheques de "
             "tercero recibidos, suele coincidir con el CUIT del partner "
             "que nos lo entregó. Se persiste explícito acá para "
             "trazabilidad de la cadena de endosos.",
    )
    issue_date = fields.Date(
        string="Fecha de emisión",
        default=fields.Date.context_today,
        help="Fecha en la que el cheque fue librado por el emisor.",
    )
    at_sight = fields.Boolean(
        string="A la vista",
        help="Cheque a la vista: pagadero contra presentación al banco, "
             "sin fecha de pago futura. Skipea el control de plazo entre "
             "emisión y fecha de pago.",
    )

    _LEGAL_DAYS_COMMON = 30
    _LEGAL_DAYS_ECHEQ = 360

    @api.constrains("issue_date", "payment_date", "at_sight", "is_echeq")
    def _check_payment_period(self):
        for chk in self:
            if chk.at_sight or not chk.issue_date or not chk.payment_date:
                continue
            if chk.payment_date < chk.issue_date:
                raise ValidationError(_(
                    "Cheque %(name)s: la fecha de pago (%(pay)s) no puede "
                    "ser anterior a la fecha de emisión (%(iss)s).",
                    name=chk.name or "—",
                    pay=chk.payment_date, iss=chk.issue_date,
                ))
            limit = self._LEGAL_DAYS_ECHEQ if chk.is_echeq else self._LEGAL_DAYS_COMMON
            delta = (chk.payment_date - chk.issue_date).days
            if delta > limit:
                tipo = "Echeq" if chk.is_echeq else "cheque común"
                raise ValidationError(_(
                    "Cheque %(name)s: %(delta)s días entre emisión y pago. "
                    "El %(tipo)s admite hasta %(limit)s días (Ley 24.452 / "
                    "Comunicación BCRA). Marcalo como 'A la vista' si querés "
                    "saltear este control.",
                    name=chk.name or "—", delta=delta, tipo=tipo, limit=limit,
                ))

    @api.constrains("name", "bank_id", "is_echeq")
    def _check_unique_check(self):
        for chk in self:
            if not chk.name or not chk.bank_id:
                continue
            dup = self.search([
                ("id", "!=", chk.id),
                ("name", "=", chk.name),
                ("bank_id", "=", chk.bank_id.id),
                ("is_echeq", "=", chk.is_echeq),
            ], limit=1)
            if dup:
                raise ValidationError(_(
                    "Ya existe un cheque con número %(name)s del banco "
                    "%(bank)s (id %(id)s). No se permiten duplicados — "
                    "cada cheque debe tener número único por banco.",
                    name=chk.name, bank=chk.bank_id.name, id=dup.id,
                ))
