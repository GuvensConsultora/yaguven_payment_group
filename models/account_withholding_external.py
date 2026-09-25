"""Retenciones practicadas FUERA de este sistema, que el acumulado RG 830
tiene que ver.

Caso de uso: convivencia con otro sistema (una migración en curso, un ERP
anterior) donde se siguen haciendo órdenes de pago con retención. El acumulado
mensual de Ganancias (`_tax_compute_all_helper`) sólo lee líneas de asiento con
`tax_line_id`; lo retenido en el otro sistema no las tiene, y un segundo pago
del mes al mismo proveedor se retendría de más.

No es un registro contable: no genera asientos ni entra en el SICORE, que se
declara desde el sistema donde se practicó la retención. Es sólo un dato para
el cálculo.
"""
from odoo import api, fields, models


class AccountWithholdingExternal(models.Model):
    _name = "account.withholding.external"
    _description = "Retención practicada en otro sistema"
    _order = "date desc, id desc"
    _check_company_auto = True

    _uniq_company_ref = models.Constraint(
        "unique(company_id, ref)",
        "Ya existe una retención externa con esta referencia de origen.",
    )

    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    partner_id = fields.Many2one(
        "res.partner",
        string="Proveedor",
        required=True,
        check_company=True,
        index=True,
    )
    date = fields.Date(string="Fecha", required=True, index=True)
    tax_id = fields.Many2one(
        "account.tax",
        string="Régimen",
        required=True,
        check_company=True,
        domain="[('is_withholding_tax', '=', True), ('type_tax_use', '=', 'purchase'),"
               " ('company_id', '=', company_id)]",
    )
    base_amount = fields.Monetary(
        string="Monto base",
        help="Base imponible bruta de esta retención, antes del mínimo no "
             "sujeto. Es la que suma el acumulado del mes.",
    )
    amount = fields.Monetary(string="Monto retenido")
    ref = fields.Char(
        string="Referencia de origen",
        required=True,
        help="Identificador del comprobante en el sistema de origen. Evita "
             "cargar dos veces la misma retención.",
    )

    @api.model
    def _get_same_period_totals(self, company, partner, tax, from_date, to_date):
        """(retenido, base) del período para el mismo régimen y proveedor.

        Mismo criterio que el acumulado sobre asientos: compañía (y sus
        hijas), código de régimen, proveedor comercial y rango de fechas.
        """
        records = self.sudo().search([
            ("company_id", "child_of", company.id),
            ("tax_id.l10n_ar_code", "=", tax.l10n_ar_code),
            ("partner_id", "=", partner.commercial_partner_id.id),
            ("date", "<=", to_date), ("date", ">=", from_date),
        ])
        return (
            sum(abs(r.amount) for r in records),
            sum(abs(r.base_amount) for r in records),
        )
