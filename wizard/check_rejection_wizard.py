"""Wizard de rechazo de cheque.

Circuito contable de un cheque de cliente rechazado por el banco:

1. Si el cheque era el único medio de pago del recibo, se cancela el
   recibo completo (reusa `account.payment.group.action_cancel`, que ya
   desconcilia y reversa vía asiento — PCGA / RT FACPCE).
2. Si el recibo mezclaba varios medios, se desconcilia y reversa
   ÚNICAMENTE la línea contable de ese cheque puntual — el resto del
   recibo sigue posteado. Las facturas que ese cheque cancelaba vuelven
   a quedar abiertas (la reconciliación real, vía
   `account.partial.reconcile`, dice exactamente cuáles son — no se
   infiere por FIFO).
3. Opcionalmente genera la Nota de Débito de gastos bancarios al
   partner, usando el wizard nativo `account.debit.note` con
   `debit_origin_id` apuntando a la primera factura afectada (por
   fecha) — es lo que ARCA necesita para validarla. Queda en
   BORRADOR a propósito: la corrección contable (pasos 1-2) no debe
   depender de que ARCA esté disponible en el momento del rechazo.
   Confirmar/pedir CAE de la ND es un paso posterior y manual.
"""
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import html_escape


class L10nLatamCheckRejectionWizard(models.TransientModel):
    _name = "l10n_latam.check.rejection.wizard"
    _description = "Cheque rechazado"

    check_id = fields.Many2one(
        "l10n_latam.check",
        string="Cheque",
        required=True,
        readonly=True,
    )
    payment_id = fields.Many2one(
        related="check_id.payment_id",
        string="Medio de pago",
        readonly=True,
    )
    partner_id = fields.Many2one(
        related="payment_id.partner_id",
        string="Cliente",
        readonly=True,
    )
    company_id = fields.Many2one(
        related="payment_id.company_id",
        readonly=True,
    )
    currency_id = fields.Many2one(
        related="company_id.currency_id",
        readonly=True,
    )
    rejection_date = fields.Date(
        string="Fecha de rechazo",
        required=True,
        default=fields.Date.context_today,
    )
    rejection_reason = fields.Char(
        string="Motivo",
        required=True,
        default="Rechazado por el banco",
    )
    charge_expenses = fields.Boolean(
        string="Cobrar gastos al cliente",
        default=True,
    )
    expense_description = fields.Char(
        string="Concepto",
        default="Gastos por rechazo de cheque",
    )
    expense_amount = fields.Monetary(
        string="Importe de gastos",
        currency_field="currency_id",
    )
    expense_account_id = fields.Many2one(
        "account.account",
        string="Cuenta de gastos a facturar",
        domain="[('account_type', 'in', ('income', 'income_other')),"
               " ('company_ids', 'in', company_id)]",
    )
    expense_tax_id = fields.Many2one(
        "account.tax",
        string="Impuesto",
        domain="[('type_tax_use', '=', 'sale'), ('company_id', '=', company_id)]",
        help="Dejar vacío si el gasto se cobra sin IVA discriminado.",
    )

    @api.constrains("charge_expenses", "expense_amount", "expense_account_id")
    def _check_expense_data(self):
        for wiz in self:
            if not wiz.charge_expenses:
                continue
            if wiz.expense_amount <= 0:
                raise UserError(_(
                    "Si vas a cobrar gastos, el importe tiene que ser "
                    "mayor a cero."
                ))
            if not wiz.expense_account_id:
                raise UserError(_(
                    "Elegí la cuenta de gastos a facturar en la Nota de "
                    "Débito."
                ))

    def _get_payment_receivable_line(self):
        self.ensure_one()
        payment = self.payment_id
        line = payment.move_id.line_ids.filtered(
            lambda l: (
                l.account_id.account_type in ("asset_receivable", "liability_payable")
                and l.partner_id == payment.partner_id
            )
        )
        if not line:
            raise UserError(_(
                "No encontré la línea de deudores/acreedores del asiento "
                "de este cheque (%s). No se puede continuar."
            ) % (self.check_id.name or payment.move_id.name))
        return line

    def _get_affected_invoice_moves(self, pay_line):
        partials = pay_line.matched_debit_ids | pay_line.matched_credit_ids
        if not partials:
            raise UserError(_(
                "El cheque %s no tiene comprobantes conciliados activos "
                "— puede que ya haya sido desconciliado o revertido antes."
            ) % (self.check_id.name or "—"))
        affected_lines = self.env["account.move.line"]
        for partial in partials:
            other = (
                partial.debit_move_id
                if partial.credit_move_id == pay_line
                else partial.credit_move_id
            )
            affected_lines |= other
        return partials, affected_lines.mapped("move_id").sorted(
            key=lambda m: (m.invoice_date or m.date, m.name or "")
        )

    def _post_note(self, record, body):
        record.message_post(
            body=Markup(body),
            subject=_("Cheque rechazado"),
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )

    def action_confirm(self):
        self.ensure_one()
        check = self.check_id
        payment = self.payment_id
        group = payment.payment_group_id

        if check.rejected:
            raise UserError(_(
                "El cheque %s ya está marcado como rechazado."
            ) % (check.name or "—"))
        if not group or group.state != "posted":
            raise UserError(_(
                "El recibo de este cheque no está confirmado — no hay "
                "nada que desconciliar."
            ))

        pay_line = self._get_payment_receivable_line()
        partials, affected_moves = self._get_affected_invoice_moves(pay_line)

        single_medio = len(group.payment_ids) == 1
        if single_medio:
            group.action_cancel()
            action_desc = _(
                "Se canceló el recibo completo (era el único medio de pago)."
            )
        else:
            pay_line.remove_move_reconcile()
            today = fields.Date.context_today(self)
            payment.move_id._reverse_moves(
                default_values_list=[{
                    "date": today,
                    "ref": _("Rechazo cheque %s") % (check.name or payment.move_id.name),
                }],
                cancel=True,
            )
            payment.write({"state": "rejected"})
            action_desc = _(
                "El recibo mezclaba varios medios de pago: se desconcilió y "
                "reversó únicamente la línea de este cheque. El resto del "
                "recibo sigue confirmado."
            )

        check.write({
            "rejected": True,
            "rejection_date": self.rejection_date,
            "rejection_reason": self.rejection_reason,
        })

        debit_note_move = False
        if self.charge_expenses and affected_moves:
            debit_note_move = self._create_expense_debit_note(affected_moves[0])

        note_body = self._build_note_body(action_desc, affected_moves, debit_note_move)
        self._post_note(check, note_body)
        self._post_note(group, note_body)
        for move in affected_moves:
            self._post_note(move, note_body)

        return {"type": "ir.actions.act_window_close"}

    def _build_note_body(self, action_desc, affected_moves, debit_note_move):
        self.ensure_one()
        parts = [
            "<p><strong>%s</strong></p>" % html_escape(_("Cheque rechazado")),
            "<p>%s</p>" % html_escape(action_desc),
            "<ul>",
            "<li>%s: %s</li>" % (
                html_escape(_("Cheque")), html_escape(self.check_id.name or "—"),
            ),
            "<li>%s: %s</li>" % (
                html_escape(_("Cliente")), html_escape(self.partner_id.display_name or "—"),
            ),
            "<li>%s: %s</li>" % (
                html_escape(_("Fecha de rechazo")), html_escape(str(self.rejection_date)),
            ),
            "<li>%s: %s</li>" % (
                html_escape(_("Motivo")), html_escape(self.rejection_reason or "—"),
            ),
            "</ul>",
        ]
        if affected_moves:
            parts.append("<p>%s</p><ul>" % html_escape(_("Facturas que vuelven a quedar abiertas:")))
            for mv in affected_moves:
                parts.append("<li>%s — saldo pendiente %s</li>" % (
                    html_escape(mv.name or "—"),
                    html_escape(str(mv.amount_residual)),
                ))
            parts.append("</ul>")
        if debit_note_move:
            tax_desc = self.expense_tax_id.name if self.expense_tax_id else _("sin IVA")
            parts.append("<p>%s: <strong>%s</strong> — %s (%s)</p>" % (
                html_escape(_("Nota de Débito por gastos generada")),
                html_escape(debit_note_move.name or "—"),
                html_escape(str(self.expense_amount)),
                html_escape(tax_desc),
            ))
        elif self.charge_expenses:
            parts.append("<p>%s</p>" % html_escape(_(
                "No se generó la Nota de Débito por gastos (no se encontró "
                "ninguna factura afectada)."
            )))
        else:
            parts.append("<p>%s</p>" % html_escape(_(
                "No se cobraron gastos administrativos por este rechazo."
            )))
        return "".join(parts)

    def _get_expense_debit_note_journal(self, origin_move):
        """Diario de venta ACTIVO para la ND de gastos.

        Si la factura origen vive en un diario histórico de migración /
        preimpreso (bloqueados para altas nuevas por la ir.rule "Camilleti:
        bloquear alta en diarios históricos"), el wizard nativo
        `account.debit.note` copia por default el diario de la factura
        origen y la creación de la ND revienta con AccessError — nadie,
        ni admin, puede dar de alta ahí. Se busca el diario de venta
        vigente de la misma compañía como reemplazo.
        """
        origin_journal = origin_move.journal_id
        name = (origin_journal.name or "").lower()
        if "hist" not in name and "migra" not in name:
            return origin_journal
        return self.env["account.journal"].search([
            ("type", "=", "sale"),
            ("company_id", "=", origin_move.company_id.id),
            ("l10n_latam_use_documents", "=", origin_journal.l10n_latam_use_documents),
            ("active", "=", True),
            ("id", "!=", origin_journal.id),
            ("name", "not ilike", "hist"),
            ("name", "not ilike", "migra"),
        ], limit=1) or origin_journal

    def _create_expense_debit_note(self, origin_move):
        self.ensure_one()
        journal = self._get_expense_debit_note_journal(origin_move)
        debit_note_wiz = self.env["account.debit.note"].with_context(
            active_model="account.move",
            active_ids=origin_move.ids,
        ).create({
            "date": self.rejection_date,
            "reason": self.expense_description,
            "copy_lines": False,
            "journal_id": journal.id if journal != origin_move.journal_id else False,
        })
        action = debit_note_wiz.create_debit()
        new_move = self.env["account.move"].browse(action["res_id"])
        new_move.write({
            "invoice_line_ids": [(0, 0, {
                "name": self.expense_description,
                "quantity": 1,
                "price_unit": self.expense_amount,
                "account_id": self.expense_account_id.id,
                "tax_ids": (
                    [(6, 0, [self.expense_tax_id.id])]
                    if self.expense_tax_id else [(5, 0, 0)]
                ),
            })],
        })
        self.check_id.write({"debit_note_id": new_move.id})
        return new_move
