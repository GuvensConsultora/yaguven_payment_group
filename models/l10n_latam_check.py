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
    is_cpd = fields.Boolean(
        string="Pago diferido (CPD)",
        help="Tildar si es Cheque de Pago Diferido en papel (art. 54 "
             "Ley 24.452). Igual que el Echeq, admite hasta 360 días "
             "entre emisión y pago, contra los 30 del cheque común "
             "(art. 23). NO mezclar con 'A la vista' — el CPD tiene "
             "fecha de pago futura cierta.",
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
    issuer_postal_code = fields.Char(
        string="C.P. emisor",
        help="Código postal del emisor del cheque. Útil para "
             "trazabilidad geográfica de la cartera de cheques de "
             "tercero (riesgo por plaza).",
    )
    rejected = fields.Boolean(
        string="Rechazado",
        readonly=True,
        copy=False,
        help="Tildado cuando el cheque fue rechazado por el banco "
             "(sin fondos, orden de no pago, etc.).",
    )
    rejection_date = fields.Date(
        string="Fecha de rechazo",
        readonly=True,
        copy=False,
    )
    rejection_reason = fields.Char(
        string="Motivo de rechazo",
        readonly=True,
        copy=False,
    )
    debit_note_id = fields.Many2one(
        "account.move",
        string="ND por gastos de rechazo",
        readonly=True,
        copy=False,
        help="Nota de débito generada al partner por los gastos "
             "bancarios/administrativos del rechazo, si correspondió "
             "cobrarlos.",
    )

    def action_reject_check(self):
        self.ensure_one()
        if self.rejected:
            raise ValidationError(_(
                "El cheque %s ya está marcado como rechazado.",
                self.name or "—",
            ))
        if not self.payment_id or self.payment_id.payment_type != "inbound":
            raise ValidationError(_(
                "Solo se puede marcar como rechazado un cheque recibido "
                "de un cliente (cobro), no un cheque propio entregado."
            ))
        return {
            "type": "ir.actions.act_window",
            "name": _("Cheque rechazado"),
            "res_model": "l10n_latam.check.rejection.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_check_id": self.id},
        }

    _LEGAL_DAYS_COMMON = 30
    _LEGAL_DAYS_ECHEQ = 360

    @api.constrains("issue_date", "payment_date", "at_sight", "is_echeq", "is_cpd")
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
            extended = chk.is_echeq or chk.is_cpd
            limit = self._LEGAL_DAYS_ECHEQ if extended else self._LEGAL_DAYS_COMMON
            delta = (chk.payment_date - chk.issue_date).days
            if delta > limit:
                if chk.is_echeq:
                    tipo = "Echeq"
                elif chk.is_cpd:
                    tipo = "cheque de pago diferido"
                else:
                    tipo = "cheque común"
                raise ValidationError(_(
                    "Cheque %(name)s: %(delta)s días entre emisión y pago. "
                    "El %(tipo)s admite hasta %(limit)s días (Ley 24.452 / "
                    "Comunicación BCRA). Si es Cheque de Pago Diferido (CPD) "
                    "marcalo como 'Pago diferido'; si es Echeq, como 'Echeq'; "
                    "si es a la vista, como 'A la vista'.",
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
