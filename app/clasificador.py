"""
Clasificador heuristico de productos por keywords sobre la descripcion.

Las categorias estan alineadas con el plan original (PLAN_CONSOLIDADOS_POR_CATEGORIA.md):
mascotas, viaje, casa-jaula, alimentadores, rejas, correas, juguetes,
camas, higiene, ropa-zapatos, pajaros, silicona, pasto.
"""

from __future__ import annotations

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
                       "automatic refilling", "food tray"]),
    ("camas", ["pet bed", "dog bed", "cat bed", "cama gat", "cama de gato",
               "cama perro", "window cat bed", "pet cushion", "nest"]),
    ("transporte", ["pet carrier", "carrier bag", "pet backpack", "backpack",
                    "transportador", "car seat", "car nest", "pet bag",
                    "bolsa para mascot", "mochila", "shoulder bag",
                    "travel bag", "pet stroller", "stroller", "pet cart",
                    "seat cover", "air box", "carrier"]),
    ("correas", ["leash", "harness", "collar", "correa", "arnes"]),
    ("juguetes", ["pet toy", "dog toy", "cat toy", "juguete", "plush",
                  "peluche", "rope toy", "cat tunnel", "tunnel toy",
                  "carousel toy"]),
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
    """Devuelve categoria detectada o None si nada matchea."""
    if not descripcion:
        return None
    d = descripcion.lower()
    for cat, keywords in REGLAS_CATEGORIA:
        for kw in keywords:
            if kw in d:
                return cat
    return None


def clasificar_lote(items: list[tuple[int, str | None]]) -> list[tuple[int, str | None]]:
    """Mapea [(id, descripcion), ...] -> [(id, categoria), ...]."""
    return [(_id, clasificar_descripcion(desc)) for _id, desc in items]
