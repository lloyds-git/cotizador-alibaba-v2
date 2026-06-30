# Importar PDF y validadores

## Importar PDF

El botón **Importar PDF** (en la pantalla de Productos) permite dar de alta productos automáticamente a partir del PDF de cotización que envía el proveedor.

**Cómo funciona:**

1. Haga clic en **Importar PDF** y seleccione el archivo.
2. El sistema lee el PDF y extrae:
   - El **proveedor / seller**.
   - La **lista de productos** con SKU, descripción, cantidad, precio, medidas y peso.
3. Clasifica cada producto en una **categoría** automáticamente (según las palabras clave configuradas).
4. Completa datos derivados cuando faltan (por ejemplo, calcula piezas por caja o CBM si hay información suficiente).
5. Guarda los productos en el catálogo. Si el proveedor no existía, lo crea.

**Recomendaciones:**

- Después de importar, revise los productos nuevos: confirme categoría, FOB y, sobre todo, **CBM** y **Pzas/40HQ**, que son necesarios para cotizar.
- Si algún producto quedó sin categoría, use *Reclasificar sin categoría* en la pantalla de **Categorias**.

## Validar CBM

El botón **Validar CBM** revisa el catálogo y muestra los productos cuyo **CBM** (metros cúbicos por caja) falta o no coincide con el que se calcula a partir de las dimensiones de cartón (`carton_dims`).

- Detecta productos **sin CBM** y productos con una **discrepancia grande** (más de ~50% de diferencia).
- Puede **aplicar** el CBM calculado a los productos seleccionados, de modo que se recalcule a partir de las medidas de la caja.

Tener un CBM correcto es importante porque determina cuántas piezas caben en un contenedor.

## Validar 40HQ

El botón **Validar 40HQ** busca productos sin **Pzas/40HQ** (piezas por contenedor de 40 pies) y propone calcularlas.

Cada producto puede estar en uno de dos estados:

- **Falta**: tiene los datos necesarios (CBM + piezas por caja) y puede calcularse automáticamente.
- **Bloqueado**: le falta CBM o piezas por caja, por lo que **no** se puede calcular hasta completar esos datos.

La fórmula utilizada es, en esencia: *piezas por contenedor ≈ (capacidad del contenedor ÷ CBM de la caja) × piezas por caja*.

Las **Pzas/40HQ** son un dato clave del cálculo: el motor reparte todos los costos del contenedor entre esas piezas para obtener el costo unitario.
