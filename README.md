# Yagüven Payment Group

Módulo Odoo 19 que agrupa múltiples `account.payment` nativos en un único documento tipo **recibo** (cobro a cliente) u **orden de pago** (pago a proveedor), construido 100% sobre primitivas nativas de Odoo, sin dependencia de ADHOC.

## ¿Qué problema resuelve?

En Argentina, un cobro o pago típico mezcla varios medios (efectivo + cheque + retención sufrida/practicada + transferencia) para cancelar una o varias facturas del mismo tercero. El wizard nativo `account.payment.register` de Odoo 19 permite aplicar un pago contra varias facturas, pero no contempla múltiples medios de pago en una sola operación con número de documento propio (recibo / OP) y su PDF.

Este módulo agrega el contenedor faltante:

- Un `account.payment.group` agrupa N `account.payment` (un registro por medio de pago).
- Concilia la contraparte contable de esos pagos contra M facturas seleccionadas del mismo tercero.
- Numeración propia separada: `REC/AAAA/NNNNNN` para cobros de clientes, `OP/AAAA/NNNNNN` para pagos a proveedores.
- Menús separados respetando la nomenclatura argentina: **Recibos** bajo Clientes, **Órdenes de pago** bajo Proveedores.

## Arquitectura

- `account.payment.group` — modelo contenedor nuevo.
- `account.payment` — se extiende únicamente con un M2O `payment_group_id`.
- Retenciones AR — se apoyan en `l10n_ar_withholding_ids` nativo de `account.payment`.
- Cheques propios / de terceros — se apoyan en `l10n_latam_check` nativo.
- Conciliación — usa `account.move.line` nativo, sin reescrituras.

No hay modelos nativos duplicados ni reescritos. Todo se hereda.

## Dependencias

- `account`
- `l10n_ar`
- `l10n_latam_check`

Probado en Odoo 19.0.

## Instalación

1. Cloná el repo dentro de tu `addons-path`:

   ```bash
   git clone https://github.com/GuvensConsultora/yaguven_payment_group.git
   ```

2. Actualizá la lista de módulos (**Apps → Update Apps List**).
3. Buscá *Yagüven Payment Group* e instalá.

## Uso

- **Recibos** (cobro a cliente): Contabilidad → Clientes → Recibos.
- **Órdenes de pago** (pago a proveedor): Contabilidad → Proveedores → Órdenes de pago.

En cada formulario:

1. Elegí el tercero y la fecha.
2. En la pestaña *Medios de pago* cargá las líneas de `account.payment` (una por cada diario / método).
3. En la pestaña *Comprobantes a cancelar* seleccioná las facturas pendientes del tercero.
4. **Confirmar** — postea los pagos, asigna número de recibo/OP y concilia contra las facturas seleccionadas.

## Estado actual

Módulo en desarrollo activo. Lo que ya está implementado:

- Modelo `account.payment.group` con cabecera, totales computados (`payments_amount`, `to_pay_amount`, `unreconciled_amount`) y estados (draft / posted / cancel).
- Vistas form / list / search con los dos menús separados (Recibos / Órdenes de pago).
- Secuencias separadas por tipo.

Roadmap pendiente:

- [ ] Lógica de `action_post` con conciliación automática contra las facturas seleccionadas.
- [ ] Manejo de diferencias (redondeo, diferencia de cambio, descuento por pronto pago).
- [ ] Reporte PDF del recibo / OP con logo y datos fiscales.
- [ ] Wizard de generación desde facturas seleccionadas (acción masiva en `account.move`).
- [ ] Validaciones cruzadas de montos y moneda entre medios de pago y facturas a cancelar.

## Filosofía del módulo

- **Nativo primero.** Cada funcionalidad se apoya en primitivas de Odoo 19 ya existentes (`account.payment`, conciliación, `l10n_ar`, `l10n_latam_check`). No se reescriben modelos ni se duplica lógica del core.
- **Sin ADHOC.** No hay ninguna dependencia del stack `ingadhoc/account-payment`. La idea es exactamente la opuesta: cubrir el caso argentino con el core.
- **Nomenclatura estricta.** *Recibo* = cobro a cliente. *Orden de pago* (OP) = pago a proveedor. No se mezclan en menús, secuencias, vistas ni reportes.

## Autor

**Yagüven C.G.** — Horacio Cerdá Chiaradía
https://yaguvencg.com.ar

## Licencia

LGPL-3
