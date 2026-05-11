"""Defaults fijos del cotizador. Hasta nuevo aviso del usuario:

    TC USD→MXN     = 20.00
    Flete marítimo = USD $5,000 por contenedor 40HQ

Resto de defaults espejados del seed de Proyecto_cotizador (config_pais MX).
Los settings persistentes en la tabla `settings` los pisan si existen.
"""
from __future__ import annotations

from decimal import Decimal

DEFAULTS: dict[str, Decimal | list[Decimal] | str] = {
    # Tipo de cambio fijo
    "tc_usd_mxn": Decimal("20.00"),

    # Flete marítimo fijo (precio_contenedor_usd)
    "flete_maritimo_usd": Decimal("5000.00"),

    # Volumen útil de un 40HQ en m³ — usado para derivar piezas_contenedor
    # cuando el row trae carton_cbm + carton_qty. Fórmula:
    #   piezas_contenedor = floor((cbm_40hq / carton_cbm) * carton_qty)
    # Default 67 = volumen empacable real (40HQ teórico ≈ 76 m³, menos
    # pérdida por estiba y huecos).
    "cbm_40hq": Decimal("67"),

    # Aranceles default (config "general" del seed)
    "dta_pct": Decimal("8.00"),
    "gastos_aduanales_pct": Decimal("5.00"),

    # Flete local hasta bodega (MXN)
    "flete_local_mxn": Decimal("0.00"),

    # País destino default
    "country_code": "MX",

    # Mexico (config_pais MX)
    # Para Lloyds dept. mascotas: descuentos comerciales 10%, descuentos NA 0%,
    # gastos fijos 24%. Total_desc = 34%. Editables en la UI por settings.
    "iva_pct": Decimal("16.00"),
    "descuentos_pct": Decimal("10.00"),
    "descuentos_na_pct": Decimal("0.00"),
    "gasto_fijo_pct": Decimal("24.00"),
    "psychological_prices_mx": [Decimal("29"), Decimal("49"), Decimal("69"), Decimal("99")],
    "psychological_prices_us": [Decimal("2.99"), Decimal("4.99"), Decimal("6.99"), Decimal("9.99")],

    # Márgenes default (tier "standard")
    "margen_nuestro_pct": Decimal("15.00"),
    "margen_cliente_pct": Decimal("30.00"),
}


def country_params(country_code: str = "MX", *, settings: dict | None = None) -> dict:
    """Build country-specific params dict, with settings override."""
    settings = settings or {}
    if country_code == "MX":
        return {
            "country_code": "MX",
            "iva_pct": _pick(settings, "iva_pct", DEFAULTS["iva_pct"]),
            "descuentos_pct": _pick(settings, "descuentos_pct", DEFAULTS["descuentos_pct"]),
            "descuentos_na_pct": _pick(settings, "descuentos_na_pct", DEFAULTS["descuentos_na_pct"]),
            "gasto_fijo_pct": _pick(settings, "gasto_fijo_pct", DEFAULTS["gasto_fijo_pct"]),
            "psychological_prices": DEFAULTS["psychological_prices_mx"],
        }
    if country_code in ("USA", "CA"):
        return {
            "country_code": country_code,
            "iva_pct": Decimal("0"),
            "descuentos_pct": Decimal("15"),
            "descuentos_na_pct": Decimal("2"),
            "gasto_fijo_pct": Decimal("20"),
            "psychological_prices": DEFAULTS["psychological_prices_us"],
        }
    raise ValueError(f"Unsupported country_code: {country_code}")


def tariff_params(tasa_arancelaria_pct: Decimal, *, settings: dict | None = None) -> dict:
    """Build tariff params dict from a tariff rate %, applying multiplier rule:
        tasa  0% → multiplicador 1.15
        tasa  5% → multiplicador 1.10
        otro    → multiplicador 1.00
    """
    settings = settings or {}
    multiplicador = _multiplier_from_rate(tasa_arancelaria_pct)
    return {
        "multiplicador_arancelario": multiplicador,
        "tasa_arancelaria_pct": tasa_arancelaria_pct,
        "dta_pct": _pick(settings, "dta_pct", DEFAULTS["dta_pct"]),
        "gastos_aduanales_pct": _pick(settings, "gastos_aduanales_pct", DEFAULTS["gastos_aduanales_pct"]),
        "precio_contenedor_usd": _pick(settings, "flete_maritimo_usd", DEFAULTS["flete_maritimo_usd"]),
    }


def _multiplier_from_rate(rate_pct: Decimal) -> Decimal:
    normalized = Decimal(rate_pct).quantize(Decimal("0.01"))
    if normalized == Decimal("0.00"):
        return Decimal("1.15")
    if normalized == Decimal("5.00"):
        return Decimal("1.10")
    return Decimal("1.00")


def _pick(settings: dict, key: str, default: Decimal) -> Decimal:
    raw = settings.get(key)
    if raw in (None, ""):
        return Decimal(str(default))
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal(str(default))
