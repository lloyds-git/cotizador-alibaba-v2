"""
Lookup unificado: consulta primero la tabla aranceles_override (DB), luego
la regla default acero/metal, y como ultimo recurso el tariffs.py estatico.

Default rule (cuando no hay override en DB ni match estatico):
  - Si material contiene 'steel', 'acero', 'metal' o 'iron' -> 35%
  - Si no -> 25%
  Fraccion arancelaria default: "—" (Salo la define despues si hace falta).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.modelos import ArancelOverride
from app.cotizador.tariffs import lookup_tariff as lookup_tariff_estatico


MATERIALES_METAL = ("steel", "acero", "metal", "iron", "hierro", "stainless", "inox")


@dataclass(frozen=True)
class TariffResult:
    fraccion: str
    tasa_pct: Decimal
    fuente: str  # "override-db", "tariffs-estatico", "default-metal", "default-25"
    nota: str = ""


def _es_metalico(material: str | None) -> bool:
    if not material:
        return False
    m = material.lower()
    return any(p in m for p in MATERIALES_METAL)


def _match_override(
    session: Session,
    categoria: str | None,
    material: str | None,
) -> ArancelOverride | None:
    """Encuentra el override mas especifico.

    Especifidad descendente:
      1. categoria == X AND material LIKE %Y%
      2. categoria == X AND material_pattern IS NULL
      3. categoria IS NULL AND material LIKE %Y%
      4. categoria IS NULL AND material_pattern IS NULL
    """
    candidatos = session.query(ArancelOverride).all()
    if not candidatos:
        return None

    mat_low = (material or "").lower()
    cat = categoria or ""

    # Filtrar candidatos aplicables y rankear
    aplica = []
    for o in candidatos:
        cat_ok = (o.categoria is None) or (o.categoria == cat)
        mat_ok = (o.material_pattern is None) or (
            mat_low and o.material_pattern.lower() in mat_low
        )
        if cat_ok and mat_ok:
            # Especifidad: cat_match (2) + mat_match (1)
            score = (2 if o.categoria is not None else 0) + (1 if o.material_pattern is not None else 0)
            aplica.append((score, o))

    if not aplica:
        return None
    aplica.sort(key=lambda x: -x[0])
    return aplica[0][1]


def resolver_arancel(
    session: Session | None,
    categoria: str | None,
    subcategoria: str | None,
    material: str | None,
) -> TariffResult:
    """Resuelve fraccion + tasa siguiendo la jerarquia:
      1. Override en DB (mas especifico gana — cat+mat > cat > mat > global)
      2. Default por material: 35% si es metalico
      3. Tariffs.py estatico (mapeo de categorias mascotas)
      4. Default 25%

    Nota: el material gana sobre el estatico para reflejar la regla de Salo:
    'todo al 25% excepto acero/metal que va al 35%'. Si quieres una tasa
    distinta para una combinacion cat+material, configura un override en
    /aranceles.
    """
    # 1. Override en DB
    if session is not None:
        ov = _match_override(session, categoria, material)
        if ov is not None:
            return TariffResult(
                fraccion=ov.fraccion,
                tasa_pct=Decimal(str(ov.tasa_pct)),
                fuente="override-db",
                nota=ov.nota or "",
            )

    # 2. Default por material metalico (gana sobre estatico)
    if _es_metalico(material):
        return TariffResult(
            fraccion="—",
            tasa_pct=Decimal("35"),
            fuente="default-metal",
            nota="Material metalico (acero/metal/iron) -> 35%. Configurar override si la fraccion real difiere.",
        )

    # 3. Tariffs.py estatico (mapeado por categorias mascotas)
    from app.cotizador.adapter import CATEGORIA_A_TARIFA
    if categoria and categoria in CATEGORIA_A_TARIFA:
        cat_tar, subcat_tar = CATEGORIA_A_TARIFA[categoria]
        entry = lookup_tariff_estatico(cat_tar, subcat_tar)
        if entry.fraccion != "—":
            return TariffResult(
                fraccion=entry.fraccion,
                tasa_pct=entry.tasa_pct,
                fuente="tariffs-estatico",
                nota=entry.nota,
            )

    # 4. Default 25%
    return TariffResult(
        fraccion="—",
        tasa_pct=Decimal("25"),
        fuente="default-25",
        nota="Tasa default 25%. Configurar override en /aranceles si aplica otra.",
    )
