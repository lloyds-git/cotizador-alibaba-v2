"""Siembra la tabla `aranceles` desde config/aranceles.yml.

Politica:
- Sin flag: solo inserta entradas faltantes (por par categoria+subcategoria).
  Preserva ediciones manuales hechas via UI.
- Con --reset: borra TODAS las filas y re-siembra desde el YAML. Util si
  quedaste con datos viejos despues de cambios al YAML.

Uso:
    python -m app.cli seed-aranceles           # via CLI (faltantes)
    python -m app.cli seed-aranceles --reset   # reset completo
    python scripts/seed_aranceles.py           # directo
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

PROYECTO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROYECTO_ROOT))

from app.db import get_session_factory, init_db
from app.modelos import Arancel

YAML_PATH = PROYECTO_ROOT / "config" / "aranceles.yml"


def cargar_yaml(path: Path = YAML_PATH) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("aranceles", [])


def seed(reset: bool = False, path: Path = YAML_PATH) -> dict:
    init_db()
    entries = cargar_yaml(path)

    Session = get_session_factory()
    with Session() as ses:
        if reset:
            borradas = ses.query(Arancel).delete()
        else:
            borradas = 0

        creadas = 0
        ya_existian = 0
        for e in entries:
            cat = e["categoria"]
            sub = e["subcategoria"]
            existe = (
                ses.query(Arancel)
                .filter_by(categoria=cat, subcategoria=sub)
                .first()
            )
            if existe is not None and not reset:
                ya_existian += 1
                continue
            ses.add(Arancel(
                categoria=cat,
                subcategoria=sub,
                fraccion=e["fraccion"],
                tasa_pct=float(e["tasa_pct"]),
                nota=e.get("nota") or None,
            ))
            creadas += 1
        ses.commit()

    return {
        "borradas": borradas,
        "creadas": creadas,
        "ya_existian": ya_existian,
        "total_yaml": len(entries),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true",
                    help="Borra toda la tabla antes de sembrar (descarta ediciones manuales)")
    args = ap.parse_args()
    r = seed(reset=args.reset)
    print(f"Seed de aranceles desde {YAML_PATH.name}:")
    if args.reset:
        print(f"  Borradas:    {r['borradas']}")
    print(f"  Creadas:     {r['creadas']}")
    print(f"  Ya existian: {r['ya_existian']}")
    print(f"  Total YAML:  {r['total_yaml']}")


if __name__ == "__main__":
    main()
