# Cotizaciones (historial)

La pantalla **Cotizaciones** muestra el historial de todas las cotizaciones guardadas del sistema. Cada registro es un **snapshot**.

## ¿Qué es un snapshot?

Un snapshot es una "foto" del cálculo de un producto en un momento dado. Guarda:

- La **fecha** y el **origen** (cómo se generó).
- Los **parámetros** usados (tipo de cambio, fletes, márgenes, descuentos).
- Los **resultados**: FOB efectivo, landed cost, venta a retailer, precio retail y **margen real** obtenido.

Sirve para tener trazabilidad: poder ver con qué supuestos se coti­zó un producto y comparar contra cálculos posteriores.

## Cómo se generan

- **Automáticamente** al exportar (origen `export-hd`, `export-interno`, etc.).
- **Al importar** un PDF (origen `import-pdf`).
- **Manualmente**, con el botón **Guardar cotización** dentro del panel de detalle de un producto.

## Filtros disponibles

- **Por archivo**: busca por el nombre del archivo de exportación.
- **Por origen**: filtra por cómo se generó (manual, exportación, importación, etc.).
- **Agrupar por archivo** vs **vista de lista**: cambia entre ver los snapshots agrupados por la exportación que los originó, o como una lista plana.

## Columnas

Foto, SKU, descripción, origen, fecha, FOB efectivo, landing (landed cost), retail en MXN, **margen real**, y el archivo asociado.

## Margen real y advertencias

El **margen real** es el margen efectivo que se obtuvo con ese cálculo. En el panel de detalle del producto, si el margen real queda **por debajo** del Margen Lloyds objetivo, el sistema lo resalta en rojo como advertencia, para que pueda revisar precios o costos.

## Restaurar un snapshot

Desde el panel de detalle de un producto puede **restaurar** un snapshot previo (vuelve a aplicar esos parámetros) o **borrarlo**.

## Visor de cotización

Cada producto puede tener un **visor de cotización** individual que muestra el desglose; cuando proviene de un PDF, además puede abrir la **cotización original** del proveedor.
