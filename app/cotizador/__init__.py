"""Cotizador 14-pasos — port del motor canónico de salo67/Proyecto_cotizador.

Toma un row de parsed_quotes (FOB USD + piezas_contenedor + tasa_arancelaria),
le aplica los 14 pasos y devuelve el desglose completo. El paso 9 (landed cost
unitario) es el ANCHOR; los pasos 10-14 derivan margen + IVA + redondeo
psicológico para llegar al precio retail final.

Public API:
    compute_for_row(row, **overrides) → dict con paso1..paso14 + meta

Modos soportados:
    - "import_china"  — desde precio FOB USD del proveedor (compute_for_row)
    - "landed_cost"   — el usuario provee paso 9 directo (compute_from_landed_cost)
    - "backward"      — el usuario provee retail MXN target, motor invierte a FOB (compute_backward)
"""
from __future__ import annotations

from .engine import (
    PricingResult,
    STEP_LABELS,
    compute_backward,
    compute_for_row,
    compute_from_landed_cost,
)
from .defaults import DEFAULTS, country_params, tariff_params
from .tariffs import lookup_tariff

__all__ = [
    "compute_for_row",
    "compute_from_landed_cost",
    "compute_backward",
    "PricingResult",
    "STEP_LABELS",
    "DEFAULTS",
    "country_params",
    "tariff_params",
    "lookup_tariff",
]
