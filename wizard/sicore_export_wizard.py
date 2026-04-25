"""Wizard de exportación SICORE Estándar Retenciones v3.0.

Genera dos TXT de posición fija requeridos por el aplicativo SICORE
de ARCA para importar retenciones del período:

- Sujetos retenidos (83 chars / registro): un registro por proveedor.
- Detalle de retenciones (198 chars / registro, campos 1 a 21):
  un registro por factura asociada a la retención (base e importe
  prorrateados al peso de cada comprobante en el group).

Adaptado del módulo de Lupatini al modelo nativo de Camilleti:
las retenciones son `account.payment.group.withholding` (no
`account.payment` con tax_withholding_id como en OCA).
"""
import base64
from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class SicoreExportWizard(models.TransientModel):
    _name = "sicore.export.wizard"
    _description = "Exportar Retenciones SICORE"

    date_from = fields.Date(
        string="Desde",
        required=True,
        default=lambda self: date.today().replace(day=1),
    )
    date_to = fields.Date(
        string="Hasta",
        required=True,
        default=fields.Date.context_today,
    )
    period = fields.Char(
        string="Período (YYYYMM)",
        compute="_compute_period",
        store=True,
    )
    tax_id = fields.Many2one(
        "account.tax",
        string="Régimen / Tax de retención",
        domain="[('l10n_ar_withholding_payment_type', '=', 'supplier'),"
               " ('l10n_ar_tax_type', 'in', ('earnings','earnings_scale'))]",
        required=True,
        help="Tax cuyo `l10n_ar_withholding_payment_type=supplier` y "
             "tipo earnings/earnings_scale. Para SICORE Ganancias.",
    )
    cod_impuesto = fields.Char(
        string="Código impuesto ARCA",
        default="217",
        help="217 = Impuesto a las Ganancias (general)\n"
             "787 = Ganancias - Relación de Dependencia",
    )

    file_txt = fields.Binary(string="TXT Retenciones", readonly=True)
    file_txt_name = fields.Char(default="SICORE_retenciones.txt")
    file_sujetos = fields.Binary(string="TXT Sujetos retenidos", readonly=True)
    file_sujetos_name = fields.Char(default="SICORE_sujetos.txt")

    state = fields.Selection(
        [("draft", "Borrador"), ("done", "Generado")],
        default="draft",
    )

    @api.depends("date_from")
    def _compute_period(self):
        for rec in self:
            rec.period = rec.date_from.strftime("%Y%m") if rec.date_from else ""

    # ── Búsqueda ──────────────────────────────────────────────────────────

    def _get_withholdings(self):
        """Retenciones del período cuyo group está posted, con la tax indicada.
        """
        domain = [
            ("tax_id", "=", self.tax_id.id),
            ("payment_group_id.state", "=", "posted"),
            ("payment_group_id.payment_date", ">=", self.date_from),
            ("payment_group_id.payment_date", "<=", self.date_to),
            ("payment_group_id.partner_type", "=", "supplier"),
        ]
        wths = self.env["account.payment.group.withholding"].search(
            domain, order="payment_group_id"
        )
        if not wths:
            raise UserError(_(
                "No se encontraron retenciones de \"%s\" entre %s y %s."
            ) % (
                self.tax_id.name,
                self.date_from.strftime("%d/%m/%Y"),
                self.date_to.strftime("%d/%m/%Y"),
            ))
        return wths

    def _get_invoices_for_withholding(self, wth):
        """Facturas de compra del group de la retención."""
        group = wth.payment_group_id
        if not group:
            return self.env["account.move"]
        return group.matched_move_line_ids.move_id.filtered(
            lambda m: m.move_type in ("in_invoice", "in_refund")
        )

    # ── Helpers de formato SICORE (posición fija) ─────────────────────────

    def _fmt_num16(self, amount):
        """Numérico 16 chars: 13 enteros + ',' + 2 decimales."""
        amount = abs(float(amount or 0))
        enteros = int(amount)
        centavos = round((amount - enteros) * 100)
        return str(enteros).zfill(13) + "," + str(centavos).zfill(2)

    def _fmt_num14(self, amount):
        """Numérico 14 chars: 11 enteros + ',' + 2 decimales."""
        amount = abs(float(amount or 0))
        enteros = int(amount)
        centavos = round((amount - enteros) * 100)
        return str(enteros).zfill(11) + "," + str(centavos).zfill(2)

    def _fmt_cuit(self, vat):
        """CUIT 11 dígitos sin guiones."""
        if not vat:
            return "00000000000"
        clean = "".join(c for c in vat if c.isdigit())
        return clean.zfill(11)[:11]

    def _get_cod_comprobante(self, inv):
        """Código de comprobante SICORE (2 chars):
        01=Factura, 02=Recibo, 03=NC, 04=ND, 06=Orden de Pago.
        """
        if not inv:
            return "06"  # anticipo sin factura
        if inv.move_type == "in_refund":
            return "03"
        doc_type = inv.l10n_latam_document_type_id
        if not doc_type:
            return "06"
        mapping = {
            "1": "06", "6": "06", "11": "06",
            "51": "06", "201": "06", "206": "06", "211": "06",
            "2": "04", "7": "04", "12": "04",
            "3": "03", "8": "03", "13": "03",
        }
        return mapping.get(str(doc_type.code or ""), "06")

    def _get_cod_condicion(self, partner):
        """Código de condición SICORE del retenido.
        01=Inscripto, 02=No inscripto, 04=Exento, 05=No alcanzado.
        Si el partner no tiene info en el padrón cargado, default 01.
        """
        padron = getattr(partner, "imp_ganancias_padron", "") or ""
        return {
            "AC": "01", "NI": "02", "EX": "04", "NA": "05",
        }.get(padron, "01")

    def _get_regimen_code(self, wth):
        """Código régimen RG 830 (3 dígitos). Toma `l10n_ar_code` de la tax."""
        code = wth.tax_id.l10n_ar_code or ""
        digits = "".join(c for c in code if c.isdigit())
        return digits.zfill(3)[:3] if digits else "   "

    # ── Constructor de registro ────────────────────────────────────────────

    def _build_record(self, wth, inv, base_inv, ret_inv):
        """Registro de 198 chars (campos 1-21) — SICORE Estándar v3.0.
        """
        group = wth.payment_group_id
        partner = group.partner_id.commercial_partner_id

        # 1. Código comprobante
        f1 = self._get_cod_comprobante(inv)
        # 2. Fecha emisión comprobante (DD/MM/AAAA)
        emit_date = inv.invoice_date if inv else group.payment_date
        f2 = emit_date.strftime("%d/%m/%Y")
        # 3. Número comprobante (12 dig + 4 espacios)
        nombre = (inv.name if inv else (wth.name or group.name)) or ""
        digits = "".join(c for c in nombre if c.isdigit())
        f3 = digits[:12].zfill(12) + "    "
        # 4. Importe comprobante (16, 13ent+,+2dec)
        f4 = self._fmt_num16(inv.amount_total if inv else group.advance_amount)
        # 5. Código impuesto (4)
        f5 = (self.cod_impuesto or "217").strip().zfill(4)[:4]
        # 6. Código régimen (3)
        f6 = self._get_regimen_code(wth)
        # 7. Código operación → 1=Retención
        f7 = "1"
        # 8. Base de cálculo (14)
        f8 = self._fmt_num14(base_inv)
        # 9. Fecha emisión retención
        f9 = group.payment_date.strftime("%d/%m/%Y")
        # 10. Código condición
        f10 = self._get_cod_condicion(partner)
        # 11. Sujeto suspendido → 0
        f11 = "0"
        # 12. Importe retención (14)
        f12 = self._fmt_num14(ret_inv)
        # 13. Porcentaje exclusión
        f13 = "000,00"
        # 14. Fecha boletín oficial → espacios
        f14 = " " * 10
        # 15. Tipo documento → 80=CUIT
        f15 = "80"
        # 16. Nro doc retenido (20: 11 + 9 espacios)
        f16 = self._fmt_cuit(partner.vat).ljust(20)
        # 17. Nro certificado original (14)
        cert = "".join(c for c in (wth.name or "") if c.isdigit())
        f17 = cert[:14].zfill(14)
        # 18. Denominación ordenante (30)
        f18 = " " * 30
        # 19. Acrecentamiento → 0
        f19 = "0"
        # 20. CUIT país retenido (11)
        f20 = " " * 11
        # 21. CUIT ordenante (11)
        f21 = " " * 11

        rec = (
            f1 + f2 + f3 + f4 + f5 + f6 + f7 + f8 + f9 + f10
            + f11 + f12 + f13 + f14 + f15 + f16 + f17
            + f18 + f19 + f20 + f21
        )
        assert len(rec) == 198, (
            "Registro SICORE longitud %d (esperado 198). "
            "Ret %s factura %s" % (
                len(rec), wth.name, inv.name if inv else "anticipo")
        )
        return rec

    def _build_sujetos_txt(self, wths):
        """TXT Sujetos: un registro por proveedor único (83 chars)."""
        seen = set()
        lines = []
        for wth in wths:
            partner = wth.payment_group_id.partner_id.commercial_partner_id
            cuit = self._fmt_cuit(partner.vat)
            if cuit in seen:
                continue
            seen.add(cuit)
            razon = (partner.name or "")[:60].ljust(60)
            cod_cond = self._get_cod_condicion(partner)
            tipo_doc = "80"
            rec = cuit + razon + cod_cond + " " * 8 + tipo_doc
            assert len(rec) == 83, (
                "Sujetos longitud %d (esperado 83). Partner %s"
                % (len(rec), partner.name)
            )
            lines.append(rec)
        return "\r\n".join(lines) + "\r\n"

    def _build_retenciones_txt(self, wths):
        """TXT Retenciones: un registro por factura asociada (198 chars).

        Si la retención es por anticipo (sin facturas), un único registro
        con el monto total. Si tiene varias facturas, prorratea base e
        importe proporcionalmente al amount_total de cada una.
        """
        lines = []
        for wth in wths:
            invoices = self._get_invoices_for_withholding(wth)
            base_total = wth.base_amount or 0.0
            ret_total = wth.amount or 0.0
            if not invoices:
                lines.append(self._build_record(wth, False, base_total, ret_total))
                continue
            total_facts = sum(abs(inv.amount_total) for inv in invoices)
            for inv in invoices:
                if total_facts:
                    prop = abs(inv.amount_total) / total_facts
                else:
                    prop = 1.0 / len(invoices)
                lines.append(self._build_record(
                    wth, inv,
                    base_total * prop, ret_total * prop,
                ))
        return "\r\n".join(lines) + "\r\n"

    def action_generate(self):
        self.ensure_one()
        if self.date_from > self.date_to:
            raise UserError(_("La fecha \"Desde\" debe ser anterior a \"Hasta\"."))

        wths = self._get_withholdings()

        sujetos = self._build_sujetos_txt(wths)
        self.file_sujetos = base64.b64encode(sujetos.encode("utf-8"))
        self.file_sujetos_name = "SICORE_sujetos_%s.txt" % self.period

        retenciones = self._build_retenciones_txt(wths)
        self.file_txt = base64.b64encode(retenciones.encode("utf-8"))
        self.file_txt_name = "SICORE_retenciones_%s.txt" % self.period

        self.state = "done"
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
