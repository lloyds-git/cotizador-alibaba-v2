"""Export/import del catalogo de un proyecto (portable entre entornos).

El "catalogo" son las tablas de configuracion que NO son datos de negocio
(productos/proveedores/fotos), sino lo que gobierna clasificacion y aranceles:

  - categorias      (+ sus keywords)   -> Categoria / CategoriaKeyword
  - aranceles                          -> Arancel
  - aranceles_override                 -> ArancelOverride
  - patrones_descarte                  -> PatronDescarte

Caso de uso: mover a produccion los aranceles/categorias que la IA investigo
en desarrollo, SIN volver a correr la IA. Se serializa a un JSON portable
(exportar_catalogo) que se reimporta en la BD de otro proyecto/entorno
(importar_catalogo) por upsert segun clave natural. La importacion nunca
borra: solo crea o actualiza, para no pisar datos que ya viviesen en destino.
"""
from __future__ import annotations

from datetime import datetime

from app.modelos import (
    Arancel,
    ArancelOverride,
    Categoria,
    CategoriaKeyword,
    PatronDescarte,
)

FORMATO = "catalogo-cotizador"
VERSION = 1


def exportar_catalogo(session, proyecto: str | None = None) -> dict:
    """Serializa el catalogo del proyecto a un dict JSON-serializable."""
    categorias = []
    for c in session.query(Categoria).order_by(Categoria.orden, Categoria.slug).all():
        kws = (
            session.query(CategoriaKeyword.keyword)
            .filter_by(categoria_id=c.id)
            .order_by(CategoriaKeyword.keyword)
            .all()
        )
        categorias.append({
            "slug": c.slug,
            "orden": c.orden,
            "fraccion": c.fraccion,
            "tasa_pct": c.tasa_pct,
            "arancel_estado": c.arancel_estado,
            "arancel_nota": c.arancel_nota,
            "arancel_fuente_url": c.arancel_fuente_url,
            "competencia_sitios": c.competencia_sitios,
            "keywords": [k for (k,) in kws],
        })

    aranceles = [
        {
            "categoria": a.categoria,
            "subcategoria": a.subcategoria,
            "fraccion": a.fraccion,
            "tasa_pct": a.tasa_pct,
            "nota": a.nota,
        }
        for a in session.query(Arancel)
        .order_by(Arancel.categoria, Arancel.subcategoria)
        .all()
    ]

    overrides = [
        {
            "categoria": o.categoria,
            "material_pattern": o.material_pattern,
            "fraccion": o.fraccion,
            "tasa_pct": o.tasa_pct,
            "nota": o.nota,
        }
        for o in session.query(ArancelOverride)
        .order_by(ArancelOverride.categoria, ArancelOverride.material_pattern)
        .all()
    ]

    patrones = [
        {"patron": p.patron, "nota": p.nota}
        for p in session.query(PatronDescarte).order_by(PatronDescarte.patron).all()
    ]

    return {
        "formato": FORMATO,
        "version": VERSION,
        "exportado_en": datetime.utcnow().isoformat(),
        "proyecto": proyecto,
        "categorias": categorias,
        "aranceles": aranceles,
        "aranceles_override": overrides,
        "patrones_descarte": patrones,
    }


class CatalogoInvalido(ValueError):
    """El JSON no tiene el formato de catalogo esperado."""


def _validar_formato(data: dict) -> None:
    if not isinstance(data, dict):
        raise CatalogoInvalido("El archivo no es un objeto JSON de catalogo.")
    if data.get("formato") != FORMATO:
        raise CatalogoInvalido(
            f"Formato desconocido: se esperaba '{FORMATO}'."
        )
    if data.get("version") != VERSION:
        raise CatalogoInvalido(
            f"Version {data.get('version')!r} no soportada (esperada {VERSION})."
        )


def importar_catalogo(session, data: dict) -> dict:
    """Upsert del catalogo desde un dict exportado. Nunca borra filas.

    Clave natural por tabla:
      - categorias: slug (reemplaza sus keywords)
      - aranceles: (categoria, subcategoria)
      - aranceles_override: (categoria, material_pattern)
      - patrones_descarte: patron

    Devuelve conteos de creadas/actualizadas por tabla.
    """
    _validar_formato(data)
    ahora = datetime.utcnow()
    res = {
        "categorias": {"creadas": 0, "actualizadas": 0},
        "aranceles": {"creadas": 0, "actualizadas": 0},
        "aranceles_override": {"creadas": 0, "actualizadas": 0},
        "patrones_descarte": {"creadas": 0, "actualizadas": 0},
    }

    # --- Categorias (+ keywords) ---
    for it in data.get("categorias") or []:
        slug = (it.get("slug") or "").strip()
        if not slug:
            continue
        cat = session.query(Categoria).filter_by(slug=slug).first()
        if cat is None:
            cat = Categoria(slug=slug)
            session.add(cat)
            res["categorias"]["creadas"] += 1
        else:
            res["categorias"]["actualizadas"] += 1
        cat.orden = it.get("orden", cat.orden if cat.orden is not None else 100)
        cat.fraccion = it.get("fraccion")
        cat.tasa_pct = it.get("tasa_pct")
        cat.arancel_estado = it.get("arancel_estado")
        cat.arancel_nota = it.get("arancel_nota")
        cat.arancel_fuente_url = it.get("arancel_fuente_url")
        cat.competencia_sitios = it.get("competencia_sitios")
        cat.arancel_actualizado_en = ahora
        session.flush()  # asegura cat.id para las keywords

        # Reemplaza keywords (normalizadas: lowercase, sin duplicados/vacias).
        limpias, vistas = [], set()
        for kw in it.get("keywords") or []:
            k = (kw or "").strip().lower()
            if k and k not in vistas:
                vistas.add(k)
                limpias.append(k)
        session.query(CategoriaKeyword).filter_by(categoria_id=cat.id).delete()
        for k in limpias:
            session.add(CategoriaKeyword(categoria_id=cat.id, keyword=k))

    # --- Aranceles (categoria, subcategoria) ---
    for it in data.get("aranceles") or []:
        categoria = (it.get("categoria") or "").strip()
        subcategoria = (it.get("subcategoria") or "").strip()
        frac = it.get("fraccion")
        tasa = it.get("tasa_pct")
        if not (categoria and subcategoria and frac and tasa is not None):
            continue
        a = (
            session.query(Arancel)
            .filter_by(categoria=categoria, subcategoria=subcategoria)
            .first()
        )
        if a is None:
            a = Arancel(categoria=categoria, subcategoria=subcategoria)
            session.add(a)
            res["aranceles"]["creadas"] += 1
        else:
            res["aranceles"]["actualizadas"] += 1
        a.fraccion = frac
        a.tasa_pct = tasa
        a.nota = it.get("nota")
        a.actualizado_en = ahora

    # --- Overrides (categoria, material_pattern) ---
    for it in data.get("aranceles_override") or []:
        frac = it.get("fraccion")
        tasa = it.get("tasa_pct")
        if not (frac and tasa is not None):
            continue
        categoria = it.get("categoria") or None
        material = it.get("material_pattern") or None
        o = (
            session.query(ArancelOverride)
            .filter_by(categoria=categoria, material_pattern=material)
            .first()
        )
        if o is None:
            o = ArancelOverride(categoria=categoria, material_pattern=material)
            session.add(o)
            res["aranceles_override"]["creadas"] += 1
        else:
            res["aranceles_override"]["actualizadas"] += 1
        o.fraccion = frac
        o.tasa_pct = tasa
        o.nota = it.get("nota")
        o.actualizado_en = ahora

    # --- Patrones de descarte (patron) ---
    for it in data.get("patrones_descarte") or []:
        patron = (it.get("patron") or "").strip()
        if not patron:
            continue
        p = session.query(PatronDescarte).filter_by(patron=patron).first()
        if p is None:
            p = PatronDescarte(patron=patron)
            session.add(p)
            res["patrones_descarte"]["creadas"] += 1
        else:
            res["patrones_descarte"]["actualizadas"] += 1
        p.nota = it.get("nota")

    session.commit()
    return res
