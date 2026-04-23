{
    "name": "Yagüven Payment Group",
    "version": "19.0.1.0.1",
    "summary": "Recibo / Orden de pago que agrupa varios account.payment contra varias facturas",
    "description": """
Agrupa múltiples account.payment nativos (efectivo, cheque, transferencia, retenciones AR)
en un único documento tipo recibo/OP argentino, cancelando una o varias facturas del mismo
partner. Construido 100% sobre primitivas nativas de Odoo 19 (account.payment,
account.move.line reconciliation, l10n_ar_withholding_ids para retenciones AR, l10n_latam_check
para cheques). Sin dependencia de ADHOC.
    """,
    "author": "Yagüven C.G.",
    "website": "https://yaguvencg.com.ar",
    "license": "LGPL-3",
    "category": "Accounting/Accounting",
    "depends": [
        "account",
        "l10n_ar",
        "l10n_latam_check",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_sequence_data.xml",
        "views/account_payment_group_views.xml",
        "views/account_payment_group_menus.xml",
    ],
    "installable": True,
    "application": False,
}
