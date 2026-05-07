"""Override de `account.move.action_register_payment`.

Cuando el usuario hace clic en el botón "Register Payment" desde una
factura posteada (in_invoice / out_invoice / *_refund), Odoo nativo abre
el wizard `account.payment.register` que crea un `account.payment`
directo, **sin payment_group**. Eso queda fuera del flujo argentino del
módulo (recibo / orden de pago, retenciones, cheques propios).

Este override redirige el botón al form de `account.payment.group` con
la factura preimputada en `to_pay_move_line_ids`. Si el usuario selecciona
varias facturas, se valida que sean del mismo `partner_id` y `partner_type`
y todas se imputan al mismo group.

Si no son del mismo partner / tipo (proveedor vs cliente), se usa el
wizard nativo como fallback — pagar facturas de partners distintos en un
solo group no tiene sentido funcional.
"""
from odoo import _, models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = "account.move"

    def action_register_payment(self):
        """Abrir el form de account.payment.group con las facturas
        imputadas, en lugar del wizard nativo de payment_register.
        """
        moves = self.filtered(lambda m: m.state == "posted")
        if not moves:
            return super().action_register_payment()

        partners = moves.mapped("partner_id.commercial_partner_id")
        if len(partners) != 1:
            raise UserError(
                _("Las facturas seleccionadas pertenecen a partners "
                  "distintos: %s. Pagá una OP por partner.")
                % ", ".join(p.name for p in partners[:5])
            )

        # Tipo de partner según move_type
        types = set()
        for m in moves:
            if m.move_type in ("in_invoice", "in_refund"):
                types.add("supplier")
            elif m.move_type in ("out_invoice", "out_refund"):
                types.add("customer")
        if len(types) != 1:
            raise UserError(
                _("Las facturas seleccionadas mezclan ventas y compras. "
                  "Generá una OP por tipo.")
            )
        partner_type = types.pop()

        # Líneas a imputar: receivable (clientes) o payable (proveedores).
        target = ("asset_receivable" if partner_type == "customer"
                  else "liability_payable")
        lines = moves.line_ids.filtered(
            lambda l: l.account_id.account_type == target
            and not l.reconciled
        )
        if not lines:
            raise UserError(
                _("Las facturas no tienen líneas pendientes a imputar.")
            )

        return {
            "name": _("Recibo / Orden de Pago"),
            "type": "ir.actions.act_window",
            "res_model": "account.payment.group",
            "view_mode": "form",
            "target": "current",
            "context": {
                "default_partner_id": partners.id,
                "default_partner_type": partner_type,
                "default_to_pay_move_line_ids": [(6, 0, lines.ids)],
                "default_company_id": moves[0].company_id.id,
            },
        }
