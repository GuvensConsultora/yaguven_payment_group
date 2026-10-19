"""Confirmación de la cancelación de un recibo / OP.

Reemplaza el `confirm` del botón: además del aviso de siempre (reversión y
desconciliación), lista las retenciones que se anulan y, si alguna es de un
mes anterior, lo que corresponde hacer en SICORE (RG 2233).
"""
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.tools.misc import html_escape


class AccountPaymentGroupCancelWizard(models.TransientModel):
    _name = "account.payment.group.cancel.wizard"
    _description = "Cancelar recibo / orden de pago"

    payment_group_id = fields.Many2one("account.payment.group", required=True, readonly=True)
    notice = fields.Html(compute="_compute_notice", sanitize=False)
    other_month = fields.Boolean(compute="_compute_notice")

    @api.depends("payment_group_id")
    def _compute_notice(self):
        today = fields.Date.context_today(self)
        for wiz in self:
            group = wiz.payment_group_id
            parts = [Markup("<p>%s</p>") % _(
                "Esto genera un asiento de reversión con fecha de hoy y "
                "desconcilia los comprobantes.")]
            other = False
            if group.state == "posted":
                for w in group.withholding_ids.filtered(lambda x: x._is_practiced()):
                    is_other, text = w._cancel_notice(today)
                    other = other or is_other
                    # ▲ naranja = requiere acción; ● azul = informativo. Nunca sólo color.
                    sym, color = ("▲", "#c75b00") if is_other else ("●", "#1f5fbf")
                    parts.append(Markup("<p style=\"color:%s;\"><strong>%s</strong> %s</p>") % (
                        color, sym, html_escape(text)))
            wiz.notice = Markup("").join(parts)
            wiz.other_month = other

    def action_confirm(self):
        self.ensure_one()
        self.payment_group_id.action_cancel()
        return {"type": "ir.actions.act_window_close"}
