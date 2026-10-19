"""Línea de retención AR aplicada a un account.payment.group.

Replica la lógica del wizard nativo `l10n_ar.payment.register.withholding`
pero persistida y atada al group, no al wizard transient.
"""
from datetime import date

from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import formatLang, html_escape


MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre")


class AccountPaymentGroupWithholding(models.Model):
    _name = "account.payment.group.withholding"
    _description = "Retención AR aplicada a un recibo / orden de pago"
    _check_company_auto = True

    # En 19 era _sql_constraints, que ya se ignoraba: la unicidad recién existe en la base desde 20.
    _uniq_group_tax = models.Constraint(
        "unique(payment_group_id, tax_id)",
        "Ya existe una retención de este impuesto en el recibo / OP. "
        "Editá la línea existente en lugar de cargarla otra vez.",
    )

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
    # Odoo 20: el tipo de pago de la retención (l10n_ar_withholding_payment_type) ya no existe;
    # la retención sufrida es un impuesto de venta y la practicada uno de compra.
    tax_type_use = fields.Char(compute="_compute_tax_type_use")
    name = fields.Char(string="Nro retención")
    tax_id = fields.Many2one(
        "account.tax",
        string="Régimen",
        required=True,
        check_company=True,
        domain="[('is_withholding_tax', '=', True), ('type_tax_use', '=', tax_type_use),"
               " ('company_id', '=', company_id)]",
    )
    withholding_sequence_id = fields.Many2one(
        related="tax_id.withholding_sequence_id"
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

    @api.depends("partner_type")
    def _compute_tax_type_use(self):
        for line in self:
            line.tax_type_use = "sale" if line.partner_type == "customer" else "purchase"

    def _compute_expected_base_amount(self):
        """Base imponible esperada según parametrización (RG 830 art. 25 —
        neto sin IVA). Sólo se usa para la verificación en chatter; el
        valor real es el que carga el cajero en `base_amount`.

        Con facturas tildadas: base = invoices_to_cancel × untaxed/total.
        Sin facturas (anticipo): base = advance_amount; fallback a
        payments_amount si advance todavía no fue cargado.
        """
        self.ensure_one()
        return self._expected_base_detail()["base"]

    def _expected_base_detail(self):
        """De dónde sale la base esperada, para mostrarlo en el chatter.

        Devuelve un dict con `base` (lo que usa la verificación) y las
        piezas de la cuenta: facturas, pagado, sin IVA, total y anticipo.
        """
        self.ensure_one()
        group = self.payment_group_id
        detail = {
            "base": 0.0, "invoices": self.env["account.move"],
            "to_cancel": 0.0, "untaxed": 0.0, "total": 0.0,
            "invoice_base": 0.0, "advance": 0.0, "fallback_payments": False,
        }
        if not group:
            return detail
        if self.tax_id.l10n_ar_withholding_tax_type == "iibb_total":
            target = group.invoices_to_cancel_amount + group.advance_amount
            detail["base"] = target or group.payments_amount
            detail["fallback_payments"] = not target
            return detail
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
        detail.update(
            invoices=inv_lines.move_id, to_cancel=group.invoices_to_cancel_amount,
            untaxed=untaxed, total=total, invoice_base=invoice_base,
            advance=advance_base,
        )
        if invoice_base or advance_base:
            detail["base"] = invoice_base + advance_base
        else:
            detail["base"] = group.payments_amount
            detail["fallback_payments"] = True
        return detail

    def _tax_compute_all_helper(self):
        """Calcula la retención del período aplicando RG 830.

        El cálculo vive en `_tax_compute_breakdown`, que además devuelve
        cada paso para el chatter: así el botón «Calcular retenciones», el
        asiento y la nota de verificación no pueden dar números distintos.

        Devuelve (amount, account_id, tax_repartition_line_id).
        """
        self.ensure_one()
        b = self._tax_compute_breakdown()
        return b["amount"], b["account_id"], b["repartition_line_id"]

    def _tax_compute_breakdown(self):
        """Cálculo RG 830 paso a paso.

        Acumulado mensual: una sola query sobre AML con `tax_line_id`
        del mismo régimen y partner en el mes calendario, leyendo
        balance (= retenciones previas) y `tax_base_amount` (= base
        previa). No iteramos `tax_ids` porque nuestro asiento no genera
        líneas dummy de base — la base vive como atributo de la línea
        de retención (patrón estilo ADHOC).

        Al acumulado se le suman las retenciones practicadas en otro
        sistema del mismo período (`account.withholding.external`): sin
        eso, durante una convivencia de sistemas el segundo pago del mes
        se retiene de más.
        """
        self.ensure_one()
        group = self.payment_group_id
        tax = self.tax_id
        is_earnings = tax.l10n_ar_withholding_tax_type in ("earnings", "earnings_scale")
        b = {
            "is_earnings": is_earnings, "from_date": False, "to_date": False,
            "base": self.base_amount or 0.0, "prev_lines": self.env["account.move.line"],
            "prev_external": self.env["account.withholding.external"],
            "prev_base": 0.0, "prev_withholdings": 0.0,
        }

        same_period_withholdings = 0.0
        if is_earnings:
            to_date = group.payment_date or date.today()
            from_date = to_date + relativedelta(day=1)
            domain = [
                ("company_id", "child_of", tax.company_id.id),
                ("parent_state", "=", "posted"),
                # En 20 sólo Ganancias de compras conserva el código ARCA (l10n_ar_withholding
                # limpia l10n_ar_code en el resto): del lado cliente se acumula por impuesto.
                ("tax_line_id.l10n_ar_code", "=", tax.l10n_ar_code)
                if tax.l10n_ar_code else ("tax_line_id", "=", tax.id),
                ("tax_line_id.l10n_ar_withholding_tax_type", "in",
                 ["earnings", "earnings_scale"]),
                ("partner_id", "=", group.partner_id.commercial_partner_id.id),
                ("date", "<=", to_date), ("date", ">=", from_date),
            ]
            prev_lines = self.env["account.move.line"].sudo().search(
                domain, order="date, id")
            same_period_withholdings = sum(
                abs(l.balance) for l in prev_lines
            )
            same_period_base = sum(
                abs(l.tax_base_amount) for l in prev_lines
            )
            external = self.env["account.withholding.external"]
            ext_records = external._get_same_period_records(
                tax.company_id, group.partner_id, tax, from_date, to_date,
            )
            ext_withholdings, ext_base = external._get_same_period_totals(
                tax.company_id, group.partner_id, tax, from_date, to_date,
            )
            same_period_withholdings += ext_withholdings
            same_period_base += ext_base
            net_amount = self.base_amount + same_period_base
            b.update(
                from_date=from_date, to_date=to_date, prev_lines=prev_lines,
                prev_external=ext_records, prev_base=same_period_base,
                prev_withholdings=same_period_withholdings,
            )
        else:
            net_amount = self.base_amount
        b["accumulated"] = net_amount
        b["non_taxable"] = tax.l10n_ar_non_taxable_amount

        net_amount = max(0, net_amount - tax.l10n_ar_non_taxable_amount)
        b["subject"] = net_amount
        # Odoo 20: compute_all descarta los impuestos de retención (l10n_account_withholding_tax
        # los filtra salvo pedido expreso) y los guarda con alícuota NEGATIVA. El importe se
        # calcula acá, siempre positivo, que es como lo carga el cajero.
        rep_line = tax.invoice_repartition_line_ids.filtered(
            lambda r: r.repartition_type == "tax")[:1]
        factor = (rep_line.factor_percent / 100.0) if rep_line else 1.0
        if tax.amount_type == "fixed":
            tax_amount = abs(tax.amount) * factor
        else:
            tax_amount = net_amount * abs(tax.amount) / 100.0 * factor
        tax_amount = group.currency_id.round(tax_amount) if group.currency_id else tax_amount
        b.update(rate=abs(tax.amount), factor=factor, amount_type=tax.amount_type,
                 account_id=rep_line.account_id.id, repartition_line_id=rep_line.id)

        if is_earnings:
            if tax.l10n_ar_withholding_tax_type == "earnings_scale":
                # 20: from_amount/fixed_amount ya no se guardan; la escala trae el cálculo.
                tax_amount = tax.l10n_ar_scale_id._l10n_ar_get_tax_amount_from_bracket(
                    net_amount)
            b["on_accumulated"] = tax_amount
            tax_amount -= same_period_withholdings
        else:
            b["on_accumulated"] = tax_amount
        b["before_minimum"] = tax_amount
        b["minimum"] = tax.l10n_ar_minimum_threshold

        if tax.l10n_ar_minimum_threshold > tax_amount:
            tax_amount = 0.0
        b["amount"] = tax_amount
        return b

    def _post_calculation_check_to_chatter(self):
        """Verifica el monto cargado contra el cálculo paramétrico y deja
        constancia en el chatter del payment_group, con el cálculo paso a
        paso: base, pagos anteriores del mes, mínimo no sujeto, alícuota o
        escala, retenido antes y mínimo de retención.

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
                b = w._tax_compute_breakdown()
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
            group.message_post(
                body=w._render_calculation_note(b),
                subject=_(
                    "Verificación de retención — %s"
                ) % (w.tax_id.name or ""),
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )

    def _render_calculation_note(self, b):
        """HTML de la nota: el cálculo renglón por renglón y debajo la
        comparación cargado / esperado. Estado con texto y símbolo, en
        azul / naranja, nunca sólo color ni verde / rojo."""
        self.ensure_one()
        group = self.payment_group_id
        currency = group.currency_id
        esc = html_escape
        num = lambda v: formatLang(self.env, v, digits=2)
        pct = lambda v: "{} %".format(formatLang(self.env, v, digits=2))
        td = "padding:2px 8px;"
        tdr = td + "text-align:right;white-space:nowrap;"
        grey = "font-size:smaller;color:#666;"
        rows = []

        def row(n, label, value, note="", strong=False):
            val = num(value) if isinstance(value, (int, float)) else (value or "")
            cell = "<strong>%s</strong>" % esc(val) if strong else esc(val)
            # str: Markup + str escaparía la nota, que ya viene escapada
            label_html = str(esc(label))
            if note:
                label_html += "<br/><span style=\"%s\">%s</span>" % (grey, note)
            rows.append(
                "<tr><td style=\"%s\">%s</td><td style=\"%s\">%s</td>"
                "<td style=\"%s\">%s</td></tr>" % (
                    td + "vertical-align:top;", esc(n), td, label_html, tdr, cell)
            )

        # 1 · base de este pago, y de dónde sale la esperada
        d = self._expected_base_detail()
        notes = []
        if d["invoices"] and d["total"]:
            notes.append(esc("Según %s: pagado %s × sin IVA %s / total %s = %s" % (
                ", ".join(d["invoices"].mapped("name")), num(d["to_cancel"]),
                num(d["untaxed"]), num(d["total"]), num(d["invoice_base"]))))
        if d["advance"]:
            notes.append(esc("Anticipo: %s" % num(d["advance"])))
        if d["fallback_payments"]:
            notes.append(esc("Sin facturas ni anticipo: se toma el total pagado"))
        row("1", _("Base de este pago (cargada)"), b["base"], "<br/>".join(notes))

        n = 1
        if b["is_earnings"]:
            # 2 · pagos anteriores del mes, uno por renglón
            prev = []
            for l in b["prev_lines"]:
                pay = l.payment_id
                op = pay.payment_group_id.name if pay and "payment_group_id" in pay._fields \
                    and pay.payment_group_id else l.move_id.name
                prev.append("%s · %s · base %s · retenido %s" % (
                    l.date.strftime("%d/%m"), op, num(abs(l.tax_base_amount)),
                    num(abs(l.balance))))
            for e in b["prev_external"]:
                prev.append("%s · %s (otro sistema) · base %s · retenido %s" % (
                    e.date.strftime("%d/%m"), e.ref, num(abs(e.base_amount)),
                    num(abs(e.amount))))
            row("2", _("Pagos anteriores del mes (bases)"), b["prev_base"],
                "<br/>".join(esc(p) for p in prev) or esc(_("— ninguno —")))
            row("3", _("Acumulado del mes (1 + 2)"), b["accumulated"])
            n = 3
        n += 1
        row(str(n), _("Menos mínimo no sujeto"), -b["non_taxable"])
        n += 1
        row(str(n), _("Sujeto a retención"), b["subject"])
        n += 1
        ref_subject = n - 1
        if self.tax_id.l10n_ar_withholding_tax_type == "earnings_scale":
            calc_label = _("Retención sobre el acumulado (según escala)")
        elif b["amount_type"] == "fixed":
            calc_label = _("Retención fija del régimen")
        else:
            calc_label = _("Retención sobre el %s (%s × %s)") % (
                _("acumulado") if b["is_earnings"] else _("sujeto"),
                ref_subject, pct(b["rate"] * b["factor"]))
        row(str(n), calc_label, b["on_accumulated"])
        ref_calc = n
        if b["is_earnings"]:
            n += 1
            row(str(n), _("Menos retenido antes en el mes"), -b["prev_withholdings"])
            n += 1
            row(str(n), _("Retención de este pago (%s − %s)") % (ref_calc, n - 1),
                b["before_minimum"])
        n += 1
        if b["minimum"] > b["before_minimum"]:
            min_note = _("%s no supera el mínimo de %s: no se retiene") % (
                num(b["before_minimum"]), num(b["minimum"]))
        else:
            min_note = _("%s supera el mínimo de %s: se retiene") % (
                num(b["before_minimum"]), num(b["minimum"]))
        row(str(n), _("Mínimo de retención"), b["amount"], esc(min_note), strong=True)

        # comparación cargado / esperado
        expected_amount = b["amount"]
        expected_base = d["base"]
        loaded_amount = self.amount or 0.0
        loaded_base = self.base_amount or 0.0
        diff_amount = loaded_amount - expected_amount
        diff_base = loaded_base - expected_base
        is_zero = (currency.is_zero if currency else (lambda v: abs(v) < 0.01))
        ok = is_zero(diff_amount) and is_zero(diff_base)
        if ok:
            color, estado = "#1f5fbf", _("● OK — lo cargado coincide con el cálculo")
        else:
            color, estado = "#c75b00", _("▲ REVISAR — lo cargado no coincide con el cálculo")

        if b["is_earnings"]:
            mes = "%s %s" % (MESES[b["to_date"].month - 1], b["to_date"].year)
        else:
            mes = group.payment_date and group.payment_date.strftime("%d/%m/%Y") or ""
        partner_label = (
            _("Retención que nos aplican")
            if group.partner_type == "customer"
            else _("Retención que aplicamos al proveedor")
        )
        cmp_rows = "".join(
            "<tr><td style=\"%s\">%s</td><td style=\"%s\">%s</td>"
            "<td style=\"%s\">%s</td><td style=\"%s\">%s</td></tr>" % (
                td, esc(lbl), tdr, esc(num(a)), tdr, esc(num(e)), tdr, esc(num(a - e)))
            for lbl, a, e in ((_("Base"), loaded_base, expected_base),
                              (_("Retenido"), loaded_amount, expected_amount))
        )
        return Markup(
            "<p><strong>%s — %s</strong><br/>%s · %s</p>"
            "<table style=\"border-collapse:collapse;\">%s</table>"
            "<p></p>"
            "<table style=\"border-collapse:collapse;\">"
            "<tr><td style=\"%s\"></td><td style=\"%s\"><strong>%s</strong></td>"
            "<td style=\"%s\"><strong>%s</strong></td><td style=\"%s\"><strong>%s</strong></td></tr>"
            "%s</table>"
            "<p style=\"color:%s;\"><strong>%s</strong></p>"
        ) % (
            esc(partner_label), esc(self.get_regimen_label()),
            esc(_("Período: %s") % mes) if mes else "",
            esc(group.partner_id.commercial_partner_id.name or ""),
            Markup("".join(rows)),
            td, tdr, esc(_("Cargado")), tdr, esc(_("Esperado")), tdr, esc(_("Diferencia")),
            Markup(cmp_rows), color, esc(estado),
        )

    def _ensure_name(self):
        for w in self.filtered(lambda x: not x.name):
            if w.tax_id.withholding_sequence_id:
                w.name = w.tax_id.withholding_sequence_id.next_by_id()
            else:
                raise UserError(_(
                    "Cargá el número de retención para %s (la tax no tiene "
                    "secuencia configurada)."
                ) % w.tax_id.name)

    # ── Helpers para el reporte certificado de retención (Anexo VIII RG 5423) ──

    def _is_ganancias(self):
        self.ensure_one()
        return self.tax_id.l10n_ar_withholding_tax_type in ("earnings", "earnings_scale")

    def _is_iibb(self):
        self.ensure_one()
        return self.tax_id.l10n_ar_withholding_tax_type in ("iibb_untaxed", "iibb_total")

    def get_withholding_type_label(self):
        self.ensure_one()
        if self._is_ganancias():
            return _("Impuesto a las Ganancias — RG 830")
        if self._is_iibb():
            return _("Ingresos Brutos")
        if self.tax_id.is_withholding_tax:
            return self.tax_id.name
        return self.tax_id.name or _("Retención")

    def get_alicuota_label(self):
        """Alícuota legible: % fijo o leyenda 'según escala'."""
        self.ensure_one()
        if self.tax_id.l10n_ar_withholding_tax_type == "earnings_scale":
            return _("Según escala (RG 830 anexo VIII)")
        # formatLang y no "{:.2f}": el separador decimal sale del idioma de
        # quien imprime (2,00 % en es_AR), igual que los importes del certificado.
        if self.base_amount:
            pct = (self.amount / self.base_amount) * 100.0
            return "{} %".format(formatLang(self.env, pct, digits=2))
        if self.tax_id.amount_type == "percent":
            return "{} %".format(formatLang(self.env, self.tax_id.amount, digits=2))
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
