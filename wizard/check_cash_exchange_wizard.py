"""Wizard de canje de cheque de cliente por efectivo.

Caso de origen (2026-11-11, Camiletti): el cliente entregó un cheque físico
en un recibo y, antes de que se depositara, volvió a pagar esa misma deuda en
efectivo y se llevó el cheque. El único botón disponible era «Cheque
rechazado», que no corresponde (el cheque no rebotó) y además, si el recibo
tenía un solo medio de pago con varios cheques adentro, lo anulaba entero.

El canje no toca el recibo original. Hace dos movimientos que se cancelan
entre sí en la cuenta del cliente:

1. Devuelve el cheque al cliente: pago de salida con el método nativo
   «Cheques de terceros devueltos» (`return_third_party_checks`) desde el
   diario donde está el cheque. Debita la cuenta del cliente y saca el
   cheque de la cartera.
2. Cobra el efectivo: recibo de cobro (talonario y turno de la caja del
   usuario, igual que un recibo cargado a mano) que se aplica a ese débito.

Resultado: la cuenta del cliente queda igual, la cartera baja en el importe
del cheque y la caja sube en el mismo importe dentro del turno abierto.
"""
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import formatLang, html_escape


class L10nLatamCheckCashExchangeWizard(models.TransientModel):
    _name = "l10n_latam.check.cash.exchange.wizard"
    _description = "Canje de cheque por efectivo"

    check_id = fields.Many2one(
        "l10n_latam.check",
        string="Cheque",
        required=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="check_id.company_id",
        readonly=True,
    )
    currency_id = fields.Many2one(
        related="check_id.currency_id",
        readonly=True,
    )
    partner_id = fields.Many2one(
        related="check_id.payment_id.partner_id",
        string="Cliente",
        readonly=True,
    )
    origin_group_id = fields.Many2one(
        related="check_id.payment_id.payment_group_id",
        string="Recibo donde entró",
        readonly=True,
    )
    amount = fields.Monetary(
        related="check_id.amount",
        string="Importe",
        readonly=True,
    )
    journal_id = fields.Many2one(
        "account.journal",
        string="Caja donde entra el efectivo",
        required=True,
        domain="[('id', 'in', allowed_journal_ids)]",
        default=lambda self: self._default_cash_journal(),
    )
    date = fields.Date(
        string="Fecha",
        required=True,
        default=fields.Date.context_today,
    )

    @api.model
    def _default_cash_journal(self):
        """Efectivo de la caja donde el usuario tiene el turno abierto.

        `cash.session` vive en otro módulo: si no está instalado, el usuario
        elige la caja a mano.
        """
        if "cash.session" not in self.env:
            return False
        session = self.env["cash.session"].sudo().search([
            ("state", "=", "open"),
            ("responsible_id", "=", self.env.user.id),
            ("company_id", "=", self.env.company.id),
        ], limit=1)
        return session.cash_register_id.journal_ids.filtered(
            lambda j: j.type == "cash")[:1]

    allowed_journal_ids = fields.Many2many(
        "account.journal",
        compute="_compute_allowed_journal_ids",
    )

    @api.model
    def _direct_cash_methods(self, journal):
        """Métodos de entrada de `journal` que suman el efectivo en el acto.

        Sin cuenta de pendientes propia, Odoo 19 usa la cuenta del diario
        (medido 2026-11-11: los cobros de Caja Central con «Caja Central
        directo» van a 1.1.1.01.002), así que también cuentan. Los de cheques
        se excluyen. Primero el que nombra la cuenta de la caja explícita.
        """
        journal = journal.sudo()
        cuenta = journal.default_account_id
        lines = journal.inbound_payment_method_line_ids.filtered(
            lambda m: "check" not in (m.code or "")
            and m.payment_account_id in (cuenta, m.payment_account_id.browse()))
        return lines.sorted(lambda m: m.payment_account_id != cuenta)

    @api.depends("company_id")
    def _compute_allowed_journal_ids(self):
        journals = self.env["account.journal"].search([("type", "=", "cash")])
        for wiz in self:
            wiz.allowed_journal_ids = journals.filtered(
                lambda j: j.company_id == wiz.company_id
                and wiz._direct_cash_methods(j))

    # ------------------------------------------------------------------
    # Validaciones
    # ------------------------------------------------------------------

    def _check_canjeable(self):
        check = self.check_id
        if check.rejected:
            raise UserError(_("El cheque %s está marcado como rechazado.") % (check.name or "—"))
        if check.cash_exchange_group_id:
            raise UserError(_(
                "El cheque %(cheque)s ya fue canjeado por efectivo en %(recibo)s.",
                cheque=check.name or "—",
                recibo=check.cash_exchange_group_id.display_name,
            ))
        if not check.payment_id or check.payment_id.payment_type != "inbound":
            raise UserError(_("Sólo se canjea un cheque recibido de un cliente."))
        if not check.current_journal_id:
            raise UserError(_(
                "El cheque %s ya no está en cartera (fue depositado, endosado o "
                "entregado). No se puede canjear.") % (check.name or "—"))

    def _return_method(self):
        method = self.check_id.current_journal_id.outbound_payment_method_line_ids.filtered(
            lambda m: m.code == "return_third_party_checks")[:1]
        if not method:
            raise UserError(_(
                "El diario «%s» no tiene el método «Cheques de terceros "
                "devueltos». Configuralo en los métodos de pago de salida "
                "del diario.") % self.check_id.current_journal_id.display_name)
        return method

    def _cash_in_method(self):
        """Método de entrada que suma el efectivo a la caja en el acto.

        Igual que en las devoluciones a clientes: el método cuya cuenta de
        pendientes ES la cuenta de la caja. Otro dejaría el cobro «en proceso».
        """
        method = self._direct_cash_methods(self.journal_id)[:1]
        if not method:
            cuenta_caja = self.journal_id.sudo().default_account_id
            raise UserError(_(
                "La caja «%(caja)s» no tiene un método de entrada que sume el "
                "efectivo en el acto (cuenta de pendientes = %(cuenta)s).",
                caja=self.journal_id.display_name,
                cuenta=cuenta_caja.display_name,
            ))
        return method

    # ------------------------------------------------------------------
    # Confirmación
    # ------------------------------------------------------------------

    def action_confirm(self):
        self.ensure_one()
        self._check_canjeable()
        check = self.check_id
        partner = self.partner_id
        Payment = self.env["account.payment"].with_company(self.company_id)

        # 1. Devolución del cheque al cliente.
        devolucion = Payment.create({
            "payment_type": "outbound",
            "partner_type": "customer",
            "partner_id": partner.id,
            "journal_id": check.current_journal_id.id,
            "payment_method_line_id": self._return_method().id,
            "amount": check.amount,
            "date": self.date,
            "l10n_latam_move_check_ids": [(6, 0, check.ids)],
            "memo": _("Canje por efectivo — devolución cheque %s") % check.name,
        })
        devolucion.action_post()
        # Odoo 19 puede diferir el asiento del pago (ver action_post del
        # recibo): si no lo generó, se fuerza.
        if not devolucion.move_id:
            devolucion._generate_journal_entry()
        if devolucion.move_id.state == "draft":
            devolucion.move_id.action_post()
        debito = devolucion.move_id.line_ids.filtered(
            lambda l: l.account_id == devolucion.destination_account_id
            and l.partner_id == partner and l.debit > 0)
        if not debito:
            raise UserError(_(
                "La devolución del cheque no generó el débito en la cuenta del "
                "cliente. No se registró nada."))

        # 2. Recibo del efectivo, aplicado a ese débito. Talonario y turno los
        # completa el alta como en un recibo cargado a mano.
        recibo = self.env["account.payment.group"].with_company(self.company_id).create({
            "partner_type": "customer",
            "payment_type": "inbound",
            "partner_id": partner.id,
            "payment_date": self.date,
            "communication": _("Canje por efectivo del cheque %(cheque)s (%(recibo)s)",
                               cheque=check.name,
                               recibo=self.origin_group_id.display_name or "—"),
            "to_pay_move_line_ids": [(6, 0, debito.ids)],
            "payment_ids": [(0, 0, {
                "payment_type": "inbound",
                "partner_type": "customer",
                "partner_id": partner.id,
                "journal_id": self.journal_id.id,
                "payment_method_line_id": self._cash_in_method().id,
                "amount": check.amount,
                "date": self.date,
                "memo": _("Canje por efectivo — cheque %s") % check.name,
            })],
        })
        recibo.action_post()

        check.write({"cash_exchange_group_id": recibo.id,
                     "cash_exchange_payment_id": devolucion.id})
        self._post_notes(devolucion, recibo)
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.payment.group",
            "res_id": recibo.id,
            "view_mode": "form",
            "target": "current",
        }

    def _post_notes(self, devolucion, recibo):
        check = self.check_id
        importe = formatLang(self.env, check.amount, currency_obj=self.currency_id)
        body = Markup(
            "<p><strong>%s</strong></p><ul>"
            "<li>%s: %s</li><li>%s: %s</li><li>%s: %s</li>"
            "<li>%s: %s</li><li>%s: %s</li></ul><p>%s</p>"
        ) % (
            html_escape(_("Cheque canjeado por efectivo")),
            html_escape(_("Cheque")), html_escape("%s — %s" % (check.name, importe)),
            html_escape(_("Cliente")), html_escape(self.partner_id.display_name),
            html_escape(_("Recibo donde había entrado")),
            html_escape(self.origin_group_id.display_name or "—"),
            html_escape(_("Devolución del cheque")), html_escape(devolucion.display_name),
            html_escape(_("Recibo del efectivo")),
            html_escape("%s — %s" % (recibo.display_name, self.journal_id.display_name)),
            html_escape(_(
                "El recibo original no se modificó. La devolución y el recibo "
                "del efectivo se cancelan entre sí: la cuenta del cliente queda "
                "igual que antes del canje.")),
        )
        for record in (check, recibo, self.origin_group_id):
            if record and hasattr(record, "message_post"):
                record.message_post(body=body, message_type="comment",
                                    subtype_xmlid="mail.mt_note")
