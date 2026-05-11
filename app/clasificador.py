"""
Clasificador heuristico de productos por keywords sobre la descripcion.

Las categorias estan alineadas con el plan original (PLAN_CONSOLIDADOS_POR_CATEGORIA.md):
mascotas, viaje, casa-jaula, alimentadores, rejas, correas, juguetes,
camas, higiene, ropa-zapatos, pajaros, silicona, pasto.

Categoria especial '_descartar': productos que en realidad son fragmentos
de pricing/empaque/notas del proveedor que Claude tomo como producto.
"""

from __future__ import annotations

import re


# Patrones que detectan filas que NO son productos (notas de pricing,
# fragmentos de empaque, etiquetas sueltas). Si la descripcion completa
# matchea cualquiera, marcamos categoria='_descartar'.
PATRONES_DESCARTAR = [
    re.compile(r"^\s*box\s+\d", re.IGNORECASE),
    re.compile(r"^\s*\d+\s+(box|stacking)\s*/?\s*$", re.IGNORECASE),
    re.compile(r"^\s*\d+\s+box\s+\d", re.IGNORECASE),
    re.compile(r"sticker label", re.IGNORECASE),
    re.compile(r"opp bag", re.IGNORECASE),
    re.compile(r"foam bag", re.IGNORECASE),
    re.compile(r"^fob ningbo", re.IGNORECASE),
    re.compile(r"^vip price", re.IGNORECASE),
    re.compile(r"^\s*\d+\.\d+\s+\d+\s*$"),  # "0.94 1"
    re.compile(r"^\s*\d+\s+\d+\s*1\s*$"),
    re.compile(r"^the filter element replacement$", re.IGNORECASE),
    re.compile(r"^remote control by app", re.IGNORECASE),
    re.compile(r"^usb (led )?set", re.IGNORECASE),
    re.compile(r"^national standard plug", re.IGNORECASE),
]

# Orden importa: primer match gana. Categorias mas especificas primero.
REGLAS_CATEGORIA = [
    # (categoria, lista de keywords minusculas a buscar en descripcion)
    # pajaros antes que alimentadores: 'hummingbird feeder' es pajaros
    ("pajaros", ["bird", "hummingbird", "pajaro", "bird nest"]),
    ("rejas", ["pet gate", "pet fence", " gate ", "fence", "puerta gat",
               "metal gate", "pet door", "puerta para"]),
    ("bebederos", ["water dispenser", "water fountain", "water feeder",
                   "bebedero", "fuente de agua", "dispenser de agua",
                   "water bottle"]),
    ("alimentadores", ["food feeder", "feeder", "comedero", "elevated bowl",
                       "pet bowl", "food storage", "food container",
                       "dog bowl", "feeding bowl", "food bowl",
                       "feeding cup", "feeding mat", "licking mat",
                       "double bowl", "slow-eating bowl", "slow eating",
                       "stainless steel bowl", "dispensador de comida",
                       "automatic refilling", "food tray",
                       "stand bowl", "single bowl", "floating bowl",
                       "feeding table", "dining table", "ceramic bowl",
                       "pet bowl", "canned lid", "can spoon", "lid and spoon",
                       "pet scissor", "single bowl", "iron frame ceramic",
                       "food storage cup", "food storage bucket"]),
    ("camas", ["pet bed", "dog bed", "cat bed", "cama gat", "cama de gato",
               "cama perro", "window cat bed", "pet cushion", "nest"]),
    ("transporte", ["pet carrier", "carrier bag", "pet backpack", "backpack",
                    "transportador", "car seat", "car nest", "pet bag",
                    "bolsa para mascot", "mochila", "shoulder bag",
                    "travel bag", "pet stroller", "stroller", "pet cart",
                    "seat cover", "air box", "carrier", "pet outings",
                    "outings suitcase", "travel bottle", "outdoor water",
                    "outdoor travel", "portable food", "portable pet"]),
    ("correas", ["leash", "harness", "collar", "correa", "arnes"]),
    ("juguetes", ["pet toy", "dog toy", "cat toy", "juguete", "plush",
                  "peluche", "rope toy", "cat tunnel", "tunnel toy",
                  "carousel toy", "catnip", "feather toy", "strawberry catnip"]),
    ("higiene", ["pet potty", "dog toilet", "litter", "arenero", "pasto sintet",
                 "cesped", "soap dispenser", "comb", "brush", "grooming",
                 "dematting", "paw cleaner", "paw washer", "paw washing",
                 "hair remover", "lint roller", "bath brush", "spray brush",
                 "bathing", "cleaning grooming"]),
    ("ropa-zapatos", ["pet clothes", "dog clothes", "pet shoes", "ropa para",
                      "zapatos para"]),
    # casa-jaula al final porque es muy generico ('plastic pet kennel'
    # matchea muchas cosas)
    ("casa-jaula", ["kennel", "dog house", "pet house", "casa de perro",
                    "jaula", "cage"]),
]


def clasificar_descripcion(descripcion: str | None) -> str | None:
    """Devuelve categoria detectada, '_descartar' si parece ruido, o None.

    Heuristicas:
    1. Descripciones muy cortas (<15 chars) o que matchean PATRONES_DESCARTAR
       -> '_descartar' (fragmento de pricing/empaque, no producto real).
    2. Match contra REGLAS_CATEGORIA en orden.
    3. None si nada calza.
    """
    if not descripcion:
        return None
    d_strip = descripcion.strip()
    if not d_strip:
        return None

    # Detectar fragmentos de pricing/empaque
    for pat in PATRONES_DESCARTAR:
        if pat.search(d_strip):
            return "_descartar"
    if len(d_strip) < 15:
        return "_descartar"

    d = d_strip.lower()
    for cat, keywords in REGLAS_CATEGORIA:
        for kw in keywords:
            if kw in d:
                return cat
    return None


def clasificar_lote(items: list[tuple[int, str | None]]) -> list[tuple[int, str | None]]:
    """Mapea [(id, descripcion), ...] -> [(id, categoria), ...]."""
    return [(_id, clasificar_descripcion(desc)) for _id, desc in items]
