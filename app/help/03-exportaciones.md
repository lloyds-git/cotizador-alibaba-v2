# Exportaciones

Desde la pantalla de Productos puede generar archivos Excel (`.xlsx`) en distintos formatos. Todos usan los **parámetros actuales** de la barra superior (tipo de cambio, márgenes, fletes, descuentos).

## Formatos para el cliente / Lloyds

### Exportar HD
Genera el formato **HD-Mascotas** para enviar al cliente, con los productos **marcados**. Aplica el motor de cotización de 14 pasos y entrega el precio calculado.

### Exportar interno
Genera un Excel **vertical** con **todas** las columnas y el detalle de los 14 pasos, pensado para uso interno de Lloyds. Usa los productos **marcados**.

### Exportar categoria
Igual que *Exportar HD*, pero exporta **todos** los productos de la **categoría seleccionada** (no depende de qué esté marcado). Requiere haber elegido una categoría en el filtro.

## Formato Pet PD (dimensiones de producto)

### Exportar PD
Genera el formato **Pet PD** (Product Dimension): datos físicos del producto (medidas, peso, empaque), pensado para marketplace. Usa los productos **marcados**. No incluye el cálculo de precios.

### Exportar PD categoria
Igual que *Exportar PD*, pero exporta **todos** los productos de la **categoría seleccionada**. Requiere haber elegido una categoría.

## ¿Cuál uso?

| Necesito… | Use |
|---|---|
| Mandar precios al cliente | **Exportar HD** (marcados) o **Exportar categoria** (toda la categoría). |
| Ver el detalle completo del cálculo | **Exportar interno**. |
| Enviar fichas de dimensiones a marketplace | **Exportar PD** / **Exportar PD categoria**. |

> Recuerde: las exportaciones por **marcados** dependen de qué productos tenga seleccionados (casilla *Cotizar*). Las exportaciones por **categoría** ignoran las marcas y toman toda la categoría.

Cada exportación que aplica el motor también queda registrada como **cotización (snapshot)** en el historial. Vea *Cotizaciones*.
