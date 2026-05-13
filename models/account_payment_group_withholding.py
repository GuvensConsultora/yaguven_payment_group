"""Línea de retención AR aplicada a un account.payment.group.

Replica la lógica del wizard nativo `l10n_ar.payment.register.withholding`
pero persistida y atada al group, no al wizard transient.
"""
from datetime import date

from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import html_escape


class AccountPaymentGroupWithholding(models.Model):
    _name = "account.payment.group.withholding"
    _description = "Retención AR aplicada a un recibo / orden de pago"
    _check_company_auto = True

    _sql_constraints = [
        ("uniq_group_tax",
         "unique(payment_group_id, tax_id)",
         "Ya existe una retención de este impuesto en el recibo / OP. "
         "Editá la línea existente en lugar de cargarla otra vez."),
    ]

    @api.onchange("tax_id")
    def _onchange_tax_id_check_duplicate(self):
        """Al elegir el impuesto en una línea nueva, si ya existe otra
        línea con el mismo tax en el mismo group, avisar al usuario y
        limpiar el campo. Más amigable que esperar al guardar."""
        for line in self:
            if not line.tax_id or not line.payment_group_id:
                continue
            duplicates = line.payment_group_id.withholding_ids.filtered(
                lambda w: w.tax_id == line.tax_id and w != line
            )
            if duplicates:
                tax_name = line.tax_id.name
                line.tax_id = False
                return {
                    "warning": {
                        "title": _("Retención duplicada"),
                        "message": _(
                            "Ya hay una retención del impuesto '%s' en este "
                            "recibo / OP. Editá la línea existente en lugar "
                            "de cargar otra."
                        ) % tax_name,
                    }
                }

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
        string="Monto base",
        help="Base imponible cargada por el cajero. En retenciones de "
             "clientes (que nos aplican a nosotros) refleja la base "
             "informada en el certificado recibido; en retenciones a "
             "proveedores refleja la base que el cajero entiende como "
             "correcta. El sistema no la recalcula: al confirmar el "
             "recibo/OP verifica contra el cálculo paramétrico y publica "
             "el resultado en el chatter.",
    )
    amount = fields.Monetary(
        string="Monto retenido",
        help="Monto retenido cargado por el cajero. No se recalcula: para "
             "cobros es lo que el cliente ya retuvo, para pagos es lo que "
             "el cajero decide retener. El sistema compara contra el "
             "cálculo según RG 830 / régimen y deja constancia en el "
             "chatter al confirmar.",
    )

    def _compute_expected_base_amount(self):
        """Base imponible esperada según parametrización (RG 830 art. 25 —
        neto sin IVA). Sólo se usa para la verificación en chatter; el
        valor real es el que carga el cajero en `base_amount`.

        Con facturas tildadas: base = invoices_to_cancel × untaxed/total.
        Sin facturas (anticipo): base = advance_amount; fallback a
        payments_amount si advance todavía no fue cargado.
        """
        self.ensure_one()
        group = self.payment_group_id
        if not group:
            return 0.0
        if self.tax_id.l10n_ar_tax_type == "iibb_total":
            target = group.invoices_to_cancel_amount + group.advance_amount
            return target or group.payments_amount
        inv_lines = group.to_pay_move_line_ids.filtered(
            lambda l: l.move_id.move_type in (
                "out_invoice", "in_invoice", "out_refund", "in_refund"
            )
        )
        untaxed = sum(inv_lines.mapped("move_id.amount_untaxed"))
        total = sum(inv_lines.mapped("move_id.amount_total"))
        invoice_base = (
            group.invoices_to_cancel_amount * untaxed / total
            if total else 0.0
        )
        advance_base = group.advance_amount
        if invoice_base or advance_base:
            return invoice_base + advance_base
        return group.payments_amount

    def _tax_compute_all_helper(self):
        """Calcula la retención del período aplicando RG 830.

        Acumulado mensual: una sola query sobre AML con `tax_line_id`
        del mismo régimen y partner en el mes calendario, leyendo
        balance (= retenciones previas) y `tax_base_amount` (= base
        previa). No iteramos `tax_ids` porque nuestro asiento no genera
        líneas dummy de base — la base vive como atributo de la línea
        de retención (patrón estilo ADHOC).

        Devuelve (amount, account_id, tax_repartition_line_id).
        """
        self.ensure_one()
        group = self.payment_group_id

        same_period_withholdings = 0.0
        if self.tax_id.l10n_ar_tax_type in ("earnings", "earnings_scale"):
            to_date = group.payment_date or date.today()
            from_date = to_date + relativedelta(day=1)
            domain = [
                ("company_id", "child_of", self.tax_id.company_id.id),
                ("parent_state", "=", "posted"),
                ("tax_line_id.l10n_ar_code", "=", self.tax_id.l10n_ar_code),
                ("tax_line_id.l10n_ar_tax_type", "in",
                 ["earnings", "earnings_scale"]),
                ("partner_id", "=", group.partner_id.commercial_partner_id.id),
                ("date", "<=", to_date), ("date", ">=", from_date),
            ]
            prev_lines = self.env["account.move.line"].sudo().search(domain)
            same_period_withholdings = sum(
                abs(l.balance) for l in prev_lines
            )
            same_period_base = sum(
                abs(l.tax_base_amount) for l in prev_lines
            )
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

    def _post_calculation_check_to_chatter(self):
        """Verifica el monto cargado contra el cálculo paramétrico y deja
        constancia en el chatter del payment_group.

        Aplica a clientes (lo que ya nos retuvieron, comparado contra la
        fórmula del régimen aplicada a la base que el cajero declara del
        certificado) y a proveedores (el cálculo que el sistema haría con
        los parámetros vigentes, contra el que el cajero decidió aplicar).

        No bloquea: si hay diferencia, registra REVISAR. La decisión de
        seguir o ajustar queda con el operador.
        """
        for w in self:
            group = w.payment_group_id
            if not group or not w.tax_id:
                continue
            try:
                expected_amount, _account, _rep = w._tax_compute_all_helper()
            except Exception as exc:
                group.message_post(
                    body=Markup(
                        "<p><strong>Verificación retención %s</strong></p>"
                        "<p>No se pudo correr el cálculo paramétrico: %s</p>"
                    ) % (
                        html_escape(w.tax_id.name or ""),
                        html_escape(str(exc)),
                    ),
                    subject=_("Verificación de retención"),
                    message_type="comment",
                    subtype_xmlid="mail.mt_note",
                )
                continue
            loaded_amount = w.amount or 0.0
            expected_base = w._compute_expected_base_amount()
            loaded_base = w.base_amount or 0.0
            currency = group.currency_id
            diff_amount = loaded_amount - expected_amount
            diff_base = loaded_base - expected_base
            ok_amount = (
                currency.is_zero(diff_amount) if currency
                else abs(diff_amount) < 0.01
            )
            ok_base = (
                currency.is_zero(diff_base) if currency
                else abs(diff_base) < 0.01
            )
            estado = "OK" if (ok_amount and ok_base) else "REVISAR"
            color = "#1e7e34" if estado == "OK" else "#b02a37"
            sym = currency.symbol if currency else ""
            fmt = lambda v: "{} {:,.2f}".format(sym, v)
            partner_label = (
                _("retención que nos aplican")
                if group.partner_type == "customer"
                else _("retención que aplicamos al proveedor")
            )
            body = (
                "<p><strong>Verificación de %s — %s</strong></p>"
                "<p>Régimen: %s</p>"
                "<table style=\"border-collapse:collapse;\">"
                "<tr><td style=\"padding:2px 8px;\"></td>"
                "<td style=\"padding:2px 8px;text-align:right;\"><strong>Cargado</strong></td>"
                "<td style=\"padding:2px 8px;text-align:right;\"><strong>Esperado</strong></td>"
                "<td style=\"padding:2px 8px;text-align:right;\"><strong>Diferencia</strong></td></tr>"
                "<tr><td style=\"padding:2px 8px;\">Base</td>"
                "<td style=\"padding:2px 8px;text-align:right;\">%s</td>"
                "<td style=\"padding:2px 8px;text-align:right;\">%s</td>"
                "<td style=\"padding:2px 8px;text-align:right;\">%s</td></tr>"
                "<tr><td style=\"padding:2px 8px;\">Monto retenido</td>"
                "<td style=\"padding:2px 8px;text-align:right;\">%s</td>"
                "<td style=\"padding:2px 8px;text-align:right;\">%s</td>"
                "<td style=\"padding:2px 8px;text-align:right;\">%s</td></tr>"
                "</table>"
                "<p style=\"color:%s;\"><strong>%s</strong></p>"
                "<p style=\"font-size:smaller;color:#666;\">Cálculo "
                "paramétrico según RG 830 / régimen y acumulado del "
                "período sobre los registros del sistema.</p>"
            ) % (
                html_escape(partner_label),
                html_escape(w.tax_id.name or ""),
                html_escape(w.get_regimen_label()),
                html_escape(fmt(loaded_base)),
                html_escape(fmt(expected_base)),
                html_escape(fmt(diff_base)),
                html_escape(fmt(loaded_amount)),
                html_escape(fmt(expected_amount)),
                html_escape(fmt(diff_amount)),
                color,
                html_escape(estado),
            )
            group.message_post(
                body=Markup(body),
                subject=_(
                    "Verificación de retención — %s"
                ) % (w.tax_id.name or ""),
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )

    def _ensure_name(self):
        for w in self.filtered(lambda x: not x.name):
            if w.tax_id.l10n_ar_withholding_sequence_id:
                w.name = w.tax_id.l10n_ar_withholding_sequence_id.next_by_id()
            else:
                raise UserError(_(
                    "Cargá el número de retención para %s (la tax no tiene "
                    "secuencia configurada)."
                ) % w.tax_id.name)

    # ── Helpers para el reporte certificado de retención (Anexo VIII RG 5423) ──

    def _is_ganancias(self):
        self.ensure_one()
        return self.tax_id.l10n_ar_tax_type in ("earnings", "earnings_scale")

    def _is_iibb(self):
        self.ensure_one()
        return self.tax_id.l10n_ar_tax_type in ("iibb_untaxed", "iibb_total")

    def get_withholding_type_label(self):
        self.ensure_one()
        if self._is_ganancias():
            return _("Impuesto a las Ganancias — RG 830")
        if self._is_iibb():
            return _("Ingresos Brutos")
        if self.tax_id.l10n_ar_withholding_payment_type:
            return self.tax_id.name
        return self.tax_id.name or _("Retención")

    def get_alicuota_label(self):
        """Alícuota legible: % fijo o leyenda 'según escala'."""
        self.ensure_one()
        if self.tax_id.l10n_ar_tax_type == "earnings_scale":
            return _("Según escala (RG 830 anexo VIII)")
        if self.base_amount:
            pct = (self.amount / self.base_amount) * 100.0
            return "{:.2f} %".format(pct)
        if self.tax_id.amount_type == "percent":
            return "{:.2f} %".format(self.tax_id.amount)
        return "—"

    def get_regimen_label(self):
        self.ensure_one()
        code = self.tax_id.l10n_ar_code or ""
        name = self.tax_id.name or ""
        if code:
            return "{} — {}".format(code, name)
        return name

    def get_certificate_invoices(self):
        """Comprobantes (facturas/NCs) asociados al group del cual proviene
        esta retención. Filtra los AML conciliados con el move del payment
        target.
        """
        self.ensure_one()
        group = self.payment_group_id
        if not group:
            return self.env["account.move"]
        return group.matched_move_line_ids.move_id.filtered(
            lambda mv: mv.move_type in (
                "out_invoice", "in_invoice", "out_refund", "in_refund"
            )
        )

    def get_certificate_op_name(self):
        self.ensure_one()
        return (
            self.payment_group_id.name
            if self.payment_group_id and self.payment_group_id.name
            else ""
        )

    def action_print_certificate(self):
        self.ensure_one()
        if self.payment_group_id.state != "posted":
            raise UserError(_(
                "El certificado de retención sólo se emite una vez "
                "confirmado el recibo / orden de pago."
            ))
        return self.env.ref(
            "yaguven_payment_group.action_report_withholding_certificate"
        ).report_action(self)
