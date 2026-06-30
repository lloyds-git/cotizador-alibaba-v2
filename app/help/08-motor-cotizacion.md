# Motor de cotización (14 pasos)

El motor calcula, para cada producto, el precio de venta partiendo del precio **FOB** del proveedor. Lo hace en **14 pasos** que puede ver en el panel de detalle de cada producto. Los pasos se recalculan en tiempo real cuando cambia cualquier parámetro de la barra superior.

## Los 14 pasos

| # | Paso | Qué hace |
|---|---|---|
| 1 | **Precio base USD** | Toma el FOB unitario del producto (incluye costos adicionales). |
| 2 | **× multiplicador arancelario** | Aplica un factor según la tasa arancelaria de la categoría. |
| 3 | **× piezas por contenedor** | Multiplica por las **Pzas/40HQ** para razonar a nivel de contenedor. |
| 4 | **+ flete marítimo USD** | Suma el flete marítimo (parámetro *Flete USD/40HQ*). |
| 5 | **+ aranceles (tasa + DTA)** | Suma los aranceles (tasa de la categoría más el DTA). |
| 6 | **+ gastos aduanales** | Suma los gastos aduanales (si *Aduanales %* es mayor que 0). |
| 7 | **× tipo de cambio** | Convierte de USD a MXN con el *TC MXN*. |
| 8 | **+ flete local MXN** | Suma el flete terrestre en México (*Flete local MXN*). |
| 9 | **÷ piezas = landed cost unitario** | Reparte todo entre las piezas: costo unitario puesto en bodega (MXN). |
| 10 | **+ descuentos + gastos fijos** | Agrega *Desc % + Desc NA % + Gastos %* sobre la venta. |
| 11 | **÷ (1 − margen Lloyds)** | Calcula la **venta a retailer** aplicando el margen de Lloyds. |
| 12 | **÷ (1 − margen retailer)** | Calcula el **precio público sin IVA** aplicando el margen del cliente. |
| 13 | **× IVA = público con IVA** | Suma el IVA para llegar al precio público final. |
| 14 | **Redondeo a precio psicológico** | Ajusta a una terminación comercial (por ejemplo `.99`). |

## Conceptos clave

- **Landed cost (paso 9)**: el costo unitario real puesto en bodega en México, antes de márgenes. Es la base sobre la que se construye el precio de venta.
- **Margen Lloyds (paso 11)** y **margen retailer (paso 12)**: se aplican como divisores `÷ (1 − margen)`, es decir, son margen sobre la venta, no sobre el costo.
- **Margen real**: una vez calculado el precio, el sistema obtiene el margen efectivo y lo compara con el Margen Lloyds objetivo. Si queda por debajo, lo marca en rojo.

## Si faltan datos

El motor necesita, como mínimo, **FOB unitario** y **Pzas/40HQ**. Si falta alguno, los pasos salen en cero y aparece una advertencia. Complete esos datos (vea *Importar PDF y validadores*) para obtener un precio válido.

## Casos imposibles

Si la suma del margen de Lloyds más los descuentos llega o supera el 100%, o si el margen del retailer llega al 100%, el cálculo es imposible y el motor lo reporta como error en lugar de dar un número incorrecto.

Vea también: *Aranceles* y *Glosario*.
