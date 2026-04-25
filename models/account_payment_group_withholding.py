"""Línea de retención AR aplicada a un account.payment.group.

Replica la lógica del wizard nativo `l10n_ar.payment.register.withholding`
pero persistida y atada al group, no al wizard transient.
"""
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountPaymentGroupWithholding(models.Model):
    _name = "account.payment.group.withholding"
    _description = "Retención AR aplicada a un recibo / orden de pago"
    _check_company_auto = True

    payment_group_id = fields.Many2one(
        "account.payment.group",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(related="payment_group_id.company_id", store=True)
    currency_id = fields.Many2one(related="payment_group_id.currency_id")
    partner_type = fields.Selection(related="payment_group_id.partner_type")
    name = fields.Char(string="Nro retención")
    tax_id = fields.Many2one(
        "account.tax",
        string="Régimen",
        required=True,
        check_company=True,
        domain="[('l10n_ar_withholding_payment_type', '=', partner_type),"
               " ('company_id', '=', company_id)]",
    )
    withholding_sequence_id = fields.Many2one(
        related="tax_id.l10n_ar_withholding_sequence_id"
    )
    base_amount = fields.Monetary(
        compute="_compute_base_amount",
        store=True,
        readonly=False,
        required=True,
    )
    amount = fields.Monetary(
        compute="_compute_amount",
        store=True,
        readonly=False,
        required=True,
    )

    @api.depends("payment_group_id.invoices_to_cancel_amount", "tax_id")
    def _compute_base_amount(self):
        """Base imponible default: total de facturas a cancelar.

        Para IIBB total se usa el bruto; para el resto, base imponible neta
        del IVA (replica el cálculo del wizard nativo: bruto * untaxed/total
        de las facturas asociadas). Si no hay facturas (anticipo), cae al
        importe total de medios de pago.
        """
        for w in self:
            group = w.payment_group_id
            if not group:
                w.base_amount = 0.0
                continue
            if w.tax_id.l10n_ar_tax_type == "iibb_total":
                w.base_amount = group.invoices_to_cancel_amount or group.payments_amount
                continue
            inv_lines = group.to_pay_move_line_ids.filtered(
                lambda l: l.move_id.move_type in (
                    "out_invoice", "in_invoice", "out_refund", "in_refund"
                )
            )
            untaxed = sum(inv_lines.mapped("move_id.amount_untaxed"))
            total = sum(inv_lines.mapped("move_id.amount_total"))
            if total:
                w.base_amount = group.invoices_to_cancel_amount * untaxed / total
            else:
                w.base_amount = group.payments_amount

    @api.depends("base_amount", "tax_id")
    def _compute_amount(self):
        for w in self:
            if not w.tax_id:
                w.amount = 0.0
            else:
                w.amount = w._tax_compute_all_helper()[0]

    def _tax_compute_all_helper(self):
        """Replica `l10n_ar.payment.register.withholding._tax_compute_all_helper`.

        Devuelve (amount, account_id, tax_repartition_line_id).
        """
        self.ensure_one()
        group = self.payment_group_id

        same_period_withholdings = 0.0
        if self.tax_id.l10n_ar_tax_type in ("earnings", "earnings_scale"):
            to_date = group.payment_date or date.today()
            from_date = to_date + relativedelta(day=1)
            domain_w = [
                ("company_id", "child_of", self.tax_id.company_id.id),
                ("parent_state", "=", "posted"),
                ("tax_line_id.l10n_ar_code", "=", self.tax_id.l10n_ar_code),
                ("tax_line_id.l10n_ar_tax_type", "in",
                 ["earnings", "earnings_scale"]),
                ("partner_id", "=", group.partner_id.commercial_partner_id.id),
                ("date", "<=", to_date), ("date", ">=", from_date),
            ]
            grp_w = self.env["account.move.line"].sudo()._read_group(
                domain_w, ["partner_id"], ["balance:sum"]
            )
            same_period_withholdings = abs(grp_w[0][1]) if grp_w else 0.0
            domain_b = [
                ("company_id", "child_of", self.tax_id.company_id.id),
                ("parent_state", "=", "posted"),
                ("tax_ids.l10n_ar_code", "=", self.tax_id.l10n_ar_code),
                ("tax_ids.l10n_ar_tax_type", "in",
                 ["earnings", "earnings_scale"]),
                ("partner_id", "=", group.partner_id.commercial_partner_id.id),
                ("date", "<=", to_date), ("date", ">=", from_date),
            ]
            grp_b = self.env["account.move.line"].sudo()._read_group(
                domain_b, ["partner_id"], ["balance:sum"]
            )
            same_period_base = abs(grp_b[0][1]) if grp_b else 0.0
            net_amount = self.base_amount + same_period_base
        else:
            net_amount = self.base_amount

        net_amount = max(0, net_amount - self.tax_id.l10n_ar_non_taxable_amount)
        taxes_res = self.tax_id.compute_all(
            net_amount,
            currency=group.currency_id,
            quantity=1.0,
            product=False,
            partner=False,
            is_refund=False,
            rounding_method="round_per_line",
        )
        tax_amount = taxes_res["taxes"][0]["amount"]
        tax_account_id = taxes_res["taxes"][0]["account_id"]
        tax_repartition_line_id = taxes_res["taxes"][0]["tax_repartition_line_id"]

        if self.tax_id.l10n_ar_tax_type in ("earnings", "earnings_scale"):
            if self.tax_id.l10n_ar_tax_type == "earnings_scale":
                escala = self.env["l10n_ar.earnings.scale.line"].search([
                    ("scale_id", "=", self.tax_id.l10n_ar_scale_id.id),
                    ("excess_amount", "<=", net_amount),
                    ("to_amount", ">", net_amount),
                ], limit=1)
                if escala:
                    tax_amount = (
                        (net_amount - escala.excess_amount)
                        * escala.percentage / 100
                    ) + escala.fixed_amount
            tax_amount -= same_period_withholdings

        if self.tax_id.l10n_ar_minimum_threshold > tax_amount:
            tax_amount = 0.0
        return tax_amount, tax_account_id, tax_repartition_line_id

    def _ensure_name(self):
        for w in self.filtered(lambda x: not x.name):
            if w.tax_id.l10n_ar_withholding_sequence_id:
                w.name = w.tax_id.l10n_ar_withholding_sequence_id.next_by_id()
            else:
                raise UserError(_(
                    "Cargá el número de retención para %s (la tax no tiene "
                    "secuencia configurada)."
                ) % w.tax_id.name)
