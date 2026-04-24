from odoo import _, fields, models


class AccountPaymentGroupBook(models.Model):
    _name = "account.payment.group.book"
    _description = "Talonario de recibo / orden de pago"
    _order = "partner_type, payment_type, name"
    _check_company_auto = True

    name = fields.Char(required=True)
    sequence_id = fields.Many2one(
        "ir.sequence",
        string="Secuencia",
        required=True,
        ondelete="restrict",
        check_company=True,
    )
    partner_type = fields.Selection(
        [("customer", "Cliente"), ("supplier", "Proveedor")],
        required=True,
    )
    payment_type = fields.Selection(
        [("inbound", "Cobro"), ("outbound", "Pago")],
        required=True,
    )
    company_id = fields.Many2one(
        "res.company",
        default=lambda self: self.env.company,
        help="Dejalo vacío para que el talonario esté disponible en todas "
             "las compañías (útil para talonarios estándar).",
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "name_company_uniq",
            "unique(name, company_id)",
            "Ya existe un talonario con ese nombre en esta compañía.",
        ),
    ]

    def name_get(self):
        result = []
        for book in self:
            label = book.name
            if book.sequence_id.prefix:
                label = f"{book.name} ({book.sequence_id.prefix})"
            result.append((book.id, label))
        return result
