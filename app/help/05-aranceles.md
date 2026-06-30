# Aranceles

La pantalla **Aranceles** controla las tasas arancelarias que el motor usa para calcular los impuestos de importación de cada producto. Hay dos capas.

## 1. Aranceles estándar

Es la tabla base de tasas, organizada por **categoría** (y subcategoría). Viene precargada desde la configuración del sistema, pero se puede editar desde la interfaz:

- **Crear / Editar / Eliminar** una tasa estándar.
- Cada renglón tiene la categoría, su fracción arancelaria y la **tasa %**.

## 2. Overrides (excepciones)

Los **overrides** son excepciones que tienen prioridad sobre la tabla estándar. Permiten ajustar la tasa para casos específicos, por ejemplo por **categoría + material**.

- **Crear / Editar / Eliminar** un override.
- Tanto la categoría como el material son opcionales, lo que permite reglas más amplias o más específicas.

## Cómo se decide la tasa de un producto

El motor resuelve la tasa en este orden de prioridad (gana la primera que aplica):

1. **Override** que coincida con la categoría/material del producto.
2. Reglas especiales de material (por ejemplo, productos de metal).
3. **Arancel estándar** de su categoría.
4. **Valor por defecto** si nada de lo anterior aplica.

## Por qué importa

La tasa arancelaria entra en el **paso 5** del motor de cotización (aranceles + DTA) y también influye en el **paso 2** (multiplicador arancelario). Una tasa mal configurada cambia el costo y, por lo tanto, el precio final. Si un producto sale con un costo inesperado, revise primero qué tasa le está aplicando.

Vea también: *Motor de cotización* y *Categorias*.
