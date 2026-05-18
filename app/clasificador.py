"""Clasificador heuristico por keywords sobre la descripcion.

Las reglas viven en config/categorias.yml (fuente de verdad). Si la BD
tiene la tabla 'categorias' poblada via 'python -m app.cli seed-categorias',
se lee de ahi (mas rapido). Si no, se lee directo del YAML.

Categoria especial '_descartar': productos que en realidad son fragmentos
de pricing/empaque/notas del proveedor que Claude tomo como producto.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import yaml


PROYECTO_ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = PROYECTO_ROOT / "config" / "categorias.yml"


class Reglas(NamedTuple):
    # (slug, [keywords]) ordenada por prioridad ('orden' asc).
    categorias: list[tuple[str, list[str]]]
    # Regex compiladas para descarte.
    patrones_descarte: list[re.Pattern]


@lru_cache(maxsize=1)
def _cargar_reglas() -> Reglas:
    """Carga reglas: BD si esta sembrada, sino YAML.

    Cacheado. Si actualizas keywords en runtime, llama invalidar_cache().
    """
    reglas_bd = _intentar_bd()
    if reglas_bd is not None:
        return reglas_bd
    return _cargar_yaml()


def _intentar_bd() -> Reglas | None:
    """Devuelve reglas desde la BD si la tabla existe y tiene filas."""
    try:
        from app.db import get_session_factory
        from app.modelos import Categoria, PatronDescarte
    except Exception:
        return None

    try:
        Session = get_session_factory()
        with Session() as ses:
            cats = (
                ses.query(Categoria)
                .order_by(Categoria.orden.asc(), Categoria.id.asc())
                .all()
            )
            if not cats:
                return None
            categorias = [
                (c.slug, [kw.keyword.lower() for kw in c.keywords])
                for c in cats
            ]
            patrones = [
                re.compile(p.patron, re.IGNORECASE)
                for p in ses.query(PatronDescarte).all()
            ]
            return Reglas(categorias=categorias, patrones_descarte=patrones)
    except Exception:
        # tabla no existe, BD bloqueada, etc.
        return None


def _cargar_yaml() -> Reglas:
    with YAML_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    cats_yaml = sorted(
        data.get("categorias", []),
        key=lambda c: (c.get("orden", 100), c["slug"]),
    )
    categorias = [
        (c["slug"], [kw.lower() for kw in c.get("keywords", []) if kw])
        for c in cats_yaml
    ]
    patrones = [
        re.compile(p["patron"], re.IGNORECASE)
        for p in data.get("patrones_descarte", [])
    ]
    return Reglas(categorias=categorias, patrones_descarte=patrones)


def invalidar_cache() -> None:
    """Limpia el cache de reglas. Usalo tras un seed o en tests."""
    _cargar_reglas.cache_clear()


def clasificar_descripcion(descripcion: str | None) -> str | None:
    """Devuelve categoria detectada, '_descartar' si parece ruido, o None.

    1. Descripciones <15 chars o que matchean patrones_descarte -> '_descartar'.
    2. Match contra keywords por orden (menor 'orden' gana).
    3. None si nada calza.
    """
    if not descripcion:
        return None
    d_strip = descripcion.strip()
    if not d_strip:
        return None

    reglas = _cargar_reglas()

    for pat in reglas.patrones_descarte:
        if pat.search(d_strip):
            return "_descartar"
    if len(d_strip) < 15:
        return "_descartar"

    d = d_strip.lower()
    for slug, keywords in reglas.categorias:
        for kw in keywords:
            if kw in d:
                return slug
    return None


def clasificar_lote(items: list[tuple[int, str | None]]) -> list[tuple[int, str | None]]:
    """Mapea [(id, descripcion), ...] -> [(id, categoria), ...]."""
    return [(_id, clasificar_descripcion(desc)) for _id, desc in items]
