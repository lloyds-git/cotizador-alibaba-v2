# Pantalla de Productos

Es la pantalla principal del sistema (el menú **Productos** o la dirección `/`). Aquí vive el catálogo completo y desde aquí se cotiza y se exporta.

## Barra superior: parámetros de cotización

Los valores de la barra superior alimentan el cálculo de **todos** los productos a la vez. Al cambiar cualquiera, las cotizaciones se recalculan en pantalla.

| Campo | Qué es | Cómo se captura |
|---|---|---|
| **TC MXN** | Tipo de cambio USD → MXN. | Número (ej. `20`). |
| **Margen Lloyds** | Margen interno de Lloyds sobre la venta al retailer. | Fracción (ej. `0.25` = 25%). |
| **Margen retailer** | Margen del cliente sobre el precio público. | Fracción (ej. `0.40` = 40%). |
| **Flete USD/40HQ** | Flete marítimo en dólares por contenedor de 40 pies. | Número (ej. `5000`). |
| **Flete local MXN** | Flete terrestre en México por contenedor. | Número (ej. `70000`). |
| **Desc %** | Descuentos comerciales. | Porcentaje entero (ej. `10`). |
| **Desc NA %** | Descuentos no aplicables / adicionales. | Porcentaje entero. |
| **Gastos %** | Gastos fijos como porcentaje. | Porcentaje entero (ej. `24`). |
| **Aduanales %** | Gastos aduanales. Si lo deja en `0`, se omite ese paso del cálculo. | Porcentaje (ej. `5`). |

> Nota: los **márgenes** se capturan como fracción (`0.25`), mientras que los **descuentos, gastos y aduanales** se capturan como porcentaje entero (`10`, `24`, `5`).

El botón **Reset** regresa los parámetros a sus valores por defecto y limpia cualquier precio retail editado a mano.

## Búsqueda y filtros

- **Buscar**: escribe en el campo para filtrar por SKU o descripción (atajo: tecla `/`).
- **Categoria**: muestra solo los productos de una categoría. El número entre paréntesis es cuántos productos hay en cada una.
- **Proveedor**: filtra por proveedor.
- **Solo marcados**: muestra únicamente los productos que tiene marcados para cotizar.

## La tabla de productos

Cada fila es un producto. Las columnas son: **Foto, SKU, Descripción, Proveedor, Categoría, FOB USD, CBM, Pzas/40HQ** y **Cotizar** (la casilla para marcar).

- **Clic en una fila** abre el panel de detalle a la derecha.
- La casilla **Cotizar** marca/desmarca ese producto individual.

## Modo edición (Editando / Bloqueado)

El botón **Editando / Bloqueado** (esquina superior derecha) activa o desactiva la edición directa en la tabla:

- En **Editando**, puede modificar la **Descripción** y el **FOB USD** directamente sobre la tabla, y la **Categoría** desde su menú desplegable.
- En **Bloqueado**, la tabla es de solo lectura (evita cambios accidentales).

## Marcar productos

- **Marcar visibles**: marca todos los productos que se ven con los filtros actuales.
- **Desmarcar visibles**: los desmarca.
- La casilla **Cotizar** de cada fila marca uno por uno.

Marcar un producto lo incluye en las exportaciones de "marcados".

## Panel de detalle

Al hacer clic en una fila se abre un panel lateral con todo el producto:

- **Datos físicos editables**: descripción, material, medidas, color, MOQ, packing, peso, dimensiones de cartón, CBM, piezas por 20ft / 40HQ / caja, N.W., G.W., lead time y notas. Los cambios se guardan al salir del campo.
- **Fotos**: puede subir una o varias imágenes del producto.
- **Costos adicionales**: conceptos extra (por ejemplo EXW→FOB, caja de color) que se suman al FOB para obtener el FOB efectivo.
- **Motor de cotización**: la tabla de los 14 pasos para ese producto (ver *Motor de cotización*).
- **Cotizaciones guardadas (snapshots)**: historial de cálculos guardados, con opción de restaurar o borrar.

## Acciones destructivas (con cuidado)

- **Borrar marcados**: elimina **todos** los productos marcados, junto con sus snapshots, costos y fotos. Es irreversible y pide confirmación.
- **Borrar proveedor**: elimina **todos** los productos del proveedor seleccionado. Pide confirmación.

Vea también: *Importar PDF y validadores*, *Exportaciones* y *Motor de cotización*.
