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
   fecha) — es lo que ARCA necesita para validarla.
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
        domain=[("account_type", "in", ("income", "income_other"))],
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

        check.write({
            "rejected": True,
            "rejection_date": self.rejection_date,
            "rejection_reason": self.rejection_reason,
        })

        note_body = (
            "<p><strong>Cheque rechazado</strong></p>"
            "<p>Cheque: %s<br/>Fecha de rechazo: %s<br/>Motivo: %s</p>"
        ) % (
            html_escape(check.name or "—"),
            html_escape(str(self.rejection_date)),
            html_escape(self.rejection_reason),
        )
        self._post_note(group, note_body)
        for move in affected_moves:
            self._post_note(move, note_body)

        if self.charge_expenses and affected_moves:
            self._create_expense_debit_note(affected_moves[0])

        return {"type": "ir.actions.act_window_close"}

    def _create_expense_debit_note(self, origin_move):
        self.ensure_one()
        debit_note_wiz = self.env["account.debit.note"].with_context(
            active_model="account.move",
            active_ids=origin_move.ids,
        ).create({
            "date": self.rejection_date,
            "reason": self.expense_description,
            "copy_lines": False,
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
        new_move.action_post()
        self.check_id.write({"debit_note_id": new_move.id})
