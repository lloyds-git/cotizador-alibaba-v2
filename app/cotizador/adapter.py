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


CBM_40HQ_REF = 67  # m³ utiles 40HQ (alineado con DEFAULTS['cbm_40hq'])
CBM_20FT_REF = 33  # m³ utiles 20ft (40HQ ~= 2x un 20ft)


def producto_a_row(p: Producto, piezas_fallback: int | None = None) -> dict:
    """Convierte un Producto a un dict compatible con compute_for_row().

    SIEMPRE basamos la cotizacion en 40HQ (67 m³).
    - Si pzas_40hq existe: lo usamos directo.
    - Si solo pzas_20ft existe: lo escalamos a 40HQ con factor 67/33 ~= 2.03.
      Esto es preferible a pasar pzas_20ft tal cual (que infla el divisor
      del paso 9 y subvalua el landed por casi 2x).
    - Si nada de eso: dejamos 0 para que el motor derive desde CBM/caja.

    Args:
        p: instancia Producto
        piezas_fallback: usado solo si nada de la DB sirve
    """
    cat, subcat = CATEGORIA_A_TARIFA.get(p.categoria or "", (None, None))

    if p.pzas_40hq and p.pzas_40hq > 0:
        piezas = p.pzas_40hq
    elif p.pzas_20ft and p.pzas_20ft > 0:
        # Escalar 20ft -> 40HQ por ratio de volumen
        piezas = int(p.pzas_20ft * CBM_40HQ_REF / CBM_20FT_REF)
    else:
        piezas = piezas_fallback or 0

    return {
        "unit_price": float(p.fob_usd) if p.fob_usd is not None else None,
        "piezas_contenedor": piezas,
        "category": cat,
        "subcategory": subcat,
        "carton_qty": None,        # nuestra tabla no tiene este campo separado
        "carton_cbm": p.cbm,        # CBM por caja
    }
