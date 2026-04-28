from odoo import fields, models


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
