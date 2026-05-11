"""Lookup de fracción arancelaria + tasa arancelaria por categoría/subcategoría.

Tabla mantenida a mano. Cubre Mascotas (categoría principal del proyecto) y
algunas subcats de Hogar/Cocina. Para combinaciones desconocidas devuelve
un default conservador (tasa 0% → multiplicador 1.15).

Fuente: TIGIE (Tarifa de la Ley de los Impuestos Generales de Importación y
Exportación) + criterios típicos para productos de mascotas.

NOTA: Estas fracciones son referencia; el valor exacto depende del producto
físico (material, función, capacidad). Para una cotización formal hay que
confirmar con el agente aduanal.
"""
from __future__ import annotations

from decimal import Decimal
from dataclasses import dataclass


@dataclass(frozen=True)
class TariffEntry:
    fraccion: str           # HS code "9405.10.99"
    tasa_pct: Decimal       # 0, 5, 10, 15, 20…
    nota: str = ""          # Disclaimer / criterio


_DEFAULT = TariffEntry(fraccion="—", tasa_pct=Decimal("0"), nota="default — confirmar con agente aduanal")


# (categoria, subcategoria) → TariffEntry
_TARIFF_MAP: dict[tuple[str, str], TariffEntry] = {
    # ─── Mascotas ──────────────────────────────────────────────────────────
    ("Mascotas", "Comederos"):       TariffEntry("3924.10.01", Decimal("15"), "plástico mesa/cocina; metal varía"),
    ("Mascotas", "Bebederos"):       TariffEntry("3924.10.01", Decimal("15"), "plástico; eléctrico cambia a 8516"),
    ("Mascotas", "Camas"):           TariffEntry("9404.90.99", Decimal("20"), "artículos de cama y similares"),
    ("Mascotas", "Cojines"):         TariffEntry("9404.90.99", Decimal("20"), "cojines/almohadas — textil"),
    ("Mascotas", "Transportadoras"): TariffEntry("4202.92.99", Decimal("15"), "bolsos/maletas para transporte"),
    ("Mascotas", "Casetas"):         TariffEntry("9403.89.99", Decimal("15"), "muebles otros materiales"),
    ("Mascotas", "Jaulas"):          TariffEntry("7323.99.99", Decimal("15"), "alambre/metal"),
    ("Mascotas", "Juguetes"):        TariffEntry("9503.00.99", Decimal("0"),  "juguetes — exención"),
    ("Mascotas", "Higiene"):         TariffEntry("9603.21.01", Decimal("15"), "cepillos/peines"),
    ("Mascotas", "Aseo"):            TariffEntry("9603.21.01", Decimal("15"), "cepillos/peines"),
    ("Mascotas", "Arena"):           TariffEntry("3917.40.99", Decimal("10"), "sanitarios plástico"),
    ("Mascotas", "Sanitarios"):      TariffEntry("3917.40.99", Decimal("10"), "sanitarios plástico"),
    ("Mascotas", "Correas"):         TariffEntry("4201.00.01", Decimal("15"), "guarniciones para animales"),
    ("Mascotas", "Collares"):        TariffEntry("4201.00.01", Decimal("15"), "guarniciones para animales"),
    ("Mascotas", "Vestidos"):        TariffEntry("6307.90.99", Decimal("20"), "textil confeccionado"),
    ("Mascotas", "Ropa"):            TariffEntry("6307.90.99", Decimal("20"), "textil confeccionado"),
    ("Mascotas", "Otros"):           _DEFAULT,

    # ─── Cocina ────────────────────────────────────────────────────────────
    ("Cocina", "Dispensadores"):     TariffEntry("3924.10.01", Decimal("15"), "plástico mesa/cocina"),
    ("Cocina", "Contenedores"):      TariffEntry("3924.10.01", Decimal("15"), "plástico mesa/cocina"),
    ("Cocina", "Almacenamiento"):    TariffEntry("3923.10.99", Decimal("15"), "envases plástico"),
    ("Cocina", "Vajilla"):           TariffEntry("6912.00.01", Decimal("20"), "cerámica/loza; vidrio cambia 7013"),
    ("Cocina", "Vasos"):             TariffEntry("7013.37.99", Decimal("15"), "vidrio para mesa"),
    ("Cocina", "Termos"):            TariffEntry("9617.00.01", Decimal("15"), "termos / vacuum flask"),
    ("Cocina", "Otros"):             _DEFAULT,

    # ─── Hogar ─────────────────────────────────────────────────────────────
    ("Hogar", "Iluminación"):        TariffEntry("9405.10.99", Decimal("15"), "lámparas"),
    ("Hogar", "Baño"):               TariffEntry("3924.90.99", Decimal("15"), "artículos plásticos baño"),
    ("Hogar", "Climatización"):      TariffEntry("8414.51.99", Decimal("15"), "ventiladores; cambia si tiene electrónica"),
    ("Hogar", "Decoración"):         TariffEntry("9505.10.99", Decimal("0"),  "decoración varía mucho"),
    ("Hogar", "Otros"):              _DEFAULT,

    # ─── Hardware / Herramientas ───────────────────────────────────────────
    ("Hardware", "Herrajes"):        TariffEntry("8302.42.99", Decimal("15"), "herrajes muebles base metal"),
    ("Hardware", "Tornillería"):     TariffEntry("7318.15.99", Decimal("10"), "tornillos/pernos hierro/acero"),
    ("Hardware", "Cerraduras"):      TariffEntry("8301.40.99", Decimal("15"), "cerraduras y candados"),
    ("Hardware", "Soportes"):        TariffEntry("8302.50.99", Decimal("15"), "soportes y ménsulas"),
    ("Hardware", "Herramientas"):    TariffEntry("8205.59.99", Decimal("15"), "herramientas de mano otros"),
    ("Hardware", "Otros"):           TariffEntry("8302.42.99", Decimal("15"), "herrajes default — confirmar"),

    # ─── Otros ─────────────────────────────────────────────────────────────
    ("Otros", "Otros"):              _DEFAULT,
    ("Otros", "Sin clasificar"):     _DEFAULT,
}


def lookup_tariff(category: str | None, subcategory: str | None) -> TariffEntry:
    """Return the tariff entry for a category/subcategory pair.

    Falls back to (category, "Otros") if subcategory not found, then to
    _DEFAULT for anything else.
    """
    if not category:
        return _DEFAULT
    cat = category.strip()
    sub = (subcategory or "").strip()
    if (cat, sub) in _TARIFF_MAP:
        return _TARIFF_MAP[(cat, sub)]
    if (cat, "Otros") in _TARIFF_MAP:
        return _TARIFF_MAP[(cat, "Otros")]
    return _DEFAULT
