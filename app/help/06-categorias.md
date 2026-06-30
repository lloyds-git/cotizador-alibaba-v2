# Categorías

La pantalla **Categorias** administra las categorías del catálogo y el clasificador automático que las asigna.

## Administrar categorías

- **Crear**: defina un *slug* (nombre en minúsculas, sin espacios, por ejemplo `casa-jaula`) y un **orden**.
- **Editar**: puede renombrar el slug; los productos asignados se actualizan.
- **Eliminar**: solo es posible si la categoría **no** tiene productos asignados.

El **orden** influye en la prioridad del clasificador automático.

## Palabras clave (keywords)

Cada categoría puede tener una lista de **palabras clave**. El clasificador automático las usa para decidir a qué categoría pertenece un producto según su descripción (al importar un PDF o al reclasificar).

> Sugerencia: si productos de cierto tipo caen en la categoría equivocada, ajuste las palabras clave de las categorías involucradas.

## Patrones de descarte

Son expresiones (regex) que sirven para **descartar** productos durante la importación; por ejemplo, líneas que no son productos reales (totales, notas, encabezados). Puede crear y eliminar patrones.

## Reclasificar sin categoría

El botón **Reclasificar sin categoría** vuelve a pasar el clasificador sobre todos los productos que quedaron **sin categoría**, aplicando las palabras clave actuales. Es útil después de importar un PDF o de ajustar las keywords.

Vea también: *Importar PDF y validadores*.
