"""
Adaptador entre los Producto de productos.db y el motor 14 pasos.

Mapea nuestras categorias minusculas (rejas, alimentadores, casa-jaula, ...)
a las categorias del lookup_tariff (Mascotas/Comederos, Mascotas/Camas, ...).
"""

from __future__ import annotations

from decimal import Decimal

from app.modelos import Producto


# Mapeo de nuestras categorias a (category, subcategory) del lookup_tariff
CATEGORIA_A_TARIFA: dict[str, tuple[str, str]] = {
    "alimentadores": ("Mascotas", "Comederos"),
    "bebederos":     ("Mascotas", "Bebederos"),
    "camas":         ("Mascotas", "Camas"),
    "casa-jaula":    ("Mascotas", "Casetas"),
    "correas":       ("Mascotas", "Correas"),
    "higiene":       ("Mascotas", "Higiene"),
    "juguetes":      ("Mascotas", "Juguetes"),
    "pajaros":       ("Mascotas", "Otros"),
    "rejas":         ("Mascotas", "Jaulas"),
    "ropa-zapatos":  ("Mascotas", "Ropa"),
    "transporte":    ("Mascotas", "Transportadoras"),
    "_descartar":    ("Otros", "Otros"),
}


def producto_a_row(p: Producto, piezas_fallback: int | None = None) -> dict:
    """Convierte un Producto a un dict compatible con compute_for_row().

    Args:
        p: instancia Producto
        piezas_fallback: si el producto no tiene pzas_40hq, se usa este valor
    """
    # Mapear categoria interna a la del lookup
    cat, subcat = CATEGORIA_A_TARIFA.get(p.categoria or "", (None, None))

    # Piezas: preferimos pzas_40hq (40HQ contenedor estandar)
    piezas = p.pzas_40hq or p.pzas_20ft or piezas_fallback or 0

    return {
        "unit_price": float(p.fob_usd) if p.fob_usd is not None else None,
        "piezas_contenedor": piezas,
        "category": cat,
        "subcategory": subcat,
        "carton_qty": None,        # nuestra tabla no tiene este campo separado
        "carton_cbm": p.cbm,        # CBM por caja
    }
