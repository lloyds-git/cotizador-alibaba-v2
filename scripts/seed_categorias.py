"""Siembra/actualiza tablas categorias + categoria_keywords + patrones_descarte
desde config/categorias.yml.

Idempotente: corre las veces que quieras, refleja el estado actual del YAML.
Estrategia: borra todos los keywords/patrones de la categoria y los reinserta.
Las categorias se upsertean por slug (preserva id para FKs si se agregan
mas adelante).

Uso:
    python -m app.cli seed-categorias        # via CLI
    python scripts/seed_categorias.py        # directo

Ambos imprimen un resumen al final.
"""

from __future__ import annotations

from pathlib import Path
import sys

import yaml

PROYECTO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROYECTO_ROOT))

from app.db import get_session_factory, init_db
from app.modelos import Categoria, CategoriaKeyword, PatronDescarte

YAML_PATH = PROYECTO_ROOT / "config" / "categorias.yml"


def cargar_yaml(path: Path = YAML_PATH) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def seed(path: Path = YAML_PATH, session_factory=None, solo_patrones: bool = False) -> dict:
    """Aplica el YAML a la BD. Devuelve resumen con contadores.

    session_factory permite sembrar en una BD concreta (p. ej. la plantilla,
    via app.db.asegurar_template). Si es None, opera sobre el proyecto por
    defecto (comportamiento historico).

    solo_patrones=True siembra unicamente los patrones_descarte (filtros de
    ruido genericos, utiles en cualquier dominio) y NO siembra categorias ni
    keywords. Se usa para la plantilla multiproyecto: los proyectos nuevos
    arrancan sin categorias (la IA las propone desde las ingestas).
    """
    if session_factory is None:
        init_db()
        session_factory = get_session_factory()
    data = cargar_yaml(path)
    cats_yaml = data.get("categorias", [])
    patrones_yaml = data.get("patrones_descarte", [])

    Session = session_factory
    cats_creadas = 0
    cats_actualizadas = 0
    cats_eliminadas = 0
    kws_total = 0
    with Session() as ses:
        if not solo_patrones:
            # Categorias: upsert por slug, reemplazar keywords
            slugs_yaml = set()
            for entry in cats_yaml:
                slug = entry["slug"]
                slugs_yaml.add(slug)
                cat = ses.query(Categoria).filter_by(slug=slug).first()
                if cat is None:
                    cat = Categoria(slug=slug, orden=entry.get("orden", 100))
                    ses.add(cat)
                    ses.flush()
                    cats_creadas += 1
                else:
                    cat.orden = entry.get("orden", 100)
                    # borrar keywords viejas
                    ses.query(CategoriaKeyword).filter_by(categoria_id=cat.id).delete()
                    cats_actualizadas += 1
                for kw in entry.get("keywords", []):
                    if not kw:
                        continue
                    ses.add(CategoriaKeyword(categoria_id=cat.id, keyword=kw.lower()))
                    kws_total += 1

            # Eliminar categorias que ya no estan en el YAML
            cats_eliminadas = (
                ses.query(Categoria)
                .filter(~Categoria.slug.in_(slugs_yaml))
                .delete(synchronize_session=False)
            )

        # Patrones: reemplazo total (no hay FK que lo impida)
        ses.query(PatronDescarte).delete()
        for p in patrones_yaml:
            ses.add(PatronDescarte(patron=p["patron"], nota=p.get("nota")))

        ses.commit()

    resumen = {
        "categorias_creadas": cats_creadas,
        "categorias_actualizadas": cats_actualizadas,
        "categorias_eliminadas": cats_eliminadas,
        "keywords_total": kws_total,
        "patrones_descarte": len(patrones_yaml),
    }
    return resumen


def main():
    resumen = seed()
    print("Seed completado desde", YAML_PATH)
    for k, v in resumen.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
