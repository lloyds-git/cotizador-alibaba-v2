"""Motor 14-pasos. Port directo de salo67/Proyecto_cotizador/backend/app/services/pricing_engine.py.

PURO: sin DB, sin HTTP, sin side effects.
DETERMINÍSTICO: mismos inputs → mismo output.
Aritmética con Decimal y rounding explícito.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext

from .defaults import DEFAULTS, country_params, tariff_params
from .tariffs import lookup_tariff

FOUR_PLACES = Decimal("0.0001")
TWO_PLACES = Decimal("0.01")
ONE = Decimal("1")
HUNDRED = Decimal("100")
ZERO = Decimal("0")

STEP_LABELS = [
    "Precio base USD",
    "× multiplicador arancelario",
    "× piezas por contenedor",
    "+ flete marítimo USD",
    "+ aranceles (tasa + DTA)",
    "+ gastos aduanales",
    "× tipo de cambio",
    "+ flete local MXN",
    "÷ piezas = landed cost unitario",
    "+ descuentos + gastos fijos",
    "÷ (1 - margen nuestro)",
    "÷ (1 - margen cliente) = público sin IVA",
    "× IVA = público con IVA",
    "Redondeo a precio psicológico",
]


@dataclass(frozen=True)
class PricingResult:
    paso1: Decimal
    paso2: Decimal
    paso3: Decimal
    paso4: Decimal
    paso5: Decimal
    paso6: Decimal
    paso7: Decimal
    paso8: Decimal
    paso9: Decimal
    paso10: Decimal
    paso11: Decimal
    paso12: Decimal
    paso13: Decimal
    paso14: Decimal
    margen_nuestro_effective: Decimal
    margen_cliente_effective: Decimal
    fraccion_arancelaria: str
    tasa_arancelaria_pct: Decimal
    tipo_cambio: Decimal
    country_code: str
    mode: str = "import_china"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {f"paso{i}": str(getattr(self, f"paso{i}")) for i in range(1, 15)}
        out.update({
            "margen_nuestro_effective": str(self.margen_nuestro_effective),
            "margen_cliente_effective": str(self.margen_cliente_effective),
            "fraccion_arancelaria": self.fraccion_arancelaria,
            "tasa_arancelaria_pct": str(self.tasa_arancelaria_pct),
            "tipo_cambio": str(self.tipo_cambio),
            "country_code": self.country_code,
            "mode": self.mode,
            "warnings": self.warnings,
        })
        # versión humana para UI
        out["steps"] = [
            {"n": i, "label": STEP_LABELS[i - 1], "value": str(getattr(self, f"paso{i}"))}
            for i in range(1, 15)
        ]
        return out


def compute_for_row(
    row: dict,
    *,
    settings: dict | None = None,
    country_code: str = "MX",
    margen_nuestro_pct: Decimal | float | str | None = None,
    margen_cliente_pct: Decimal | float | str | None = None,
    override_tasa_pct: Decimal | float | str | None = None,
    override_piezas_contenedor: int | None = None,
    override_tc: Decimal | float | str | None = None,
    override_flete_maritimo_usd: Decimal | float | str | None = None,
) -> PricingResult:
    """Run 14 pasos sobre un row de parsed_quotes.

    Inputs requeridos del row:
        - unit_price (USD)
        - piezas_contenedor (int) — fallback override_piezas_contenedor
        - category, subcategory (para derivar tasa arancelaria — fallback override_tasa_pct)

    Si falta algo crítico devuelve PricingResult con warnings y pasos en 0.
    """
    settings = settings or {}
    warnings: list[str] = []

    # FOB USD
    raw_price = row.get("unit_price")
    try:
        precio_base = Decimal(str(raw_price)) if raw_price not in (None, "") else ZERO
    except Exception:
        precio_base = ZERO
        warnings.append(f"unit_price inválido: {raw_price!r}")

    # Piezas por 40HQ — preferencia: override > campo explícito > derivado
    # de carton_qty + carton_cbm (fórmula floor((cbm_40hq / carton_cbm) * carton_qty)).
    piezas_raw = override_piezas_contenedor if override_piezas_contenedor is not None else row.get("piezas_contenedor")
    try:
        piezas = int(piezas_raw) if piezas_raw not in (None, "", 0, "0") else 0
    except Exception:
        piezas = 0
    if piezas <= 0:
        derived = _derive_piezas_from_cbm(row, settings)
        if derived > 0:
            piezas = derived
            warnings.append(f"piezas_contenedor derivado de carton_qty + carton_cbm = {piezas}")

    # Tasa arancelaria
    if override_tasa_pct is not None:
        try:
            tasa = Decimal(str(override_tasa_pct))
            entry_fraccion = row.get("fraccion_arancelaria") or "—"
        except Exception:
            tasa = ZERO
            entry_fraccion = "—"
    elif row.get("tasa_arancelaria_pct") is not None:
        tasa = Decimal(str(row["tasa_arancelaria_pct"]))
        entry_fraccion = row.get("fraccion_arancelaria") or "—"
    else:
        entry = lookup_tariff(row.get("category"), row.get("subcategory"))
        tasa = entry.tasa_pct
        entry_fraccion = entry.fraccion

    # TC y flete marítimo
    tc = _to_decimal(override_tc) if override_tc is not None else _to_decimal(settings.get("tc_usd_mxn", DEFAULTS["tc_usd_mxn"]))
    flete_maritimo = (
        _to_decimal(override_flete_maritimo_usd) if override_flete_maritimo_usd is not None
        else _to_decimal(settings.get("flete_maritimo_usd", DEFAULTS["flete_maritimo_usd"]))
    )

    # Country params
    cp = country_params(country_code, settings=settings)

    # Tariff params (multiplier rule + DTA + gastos + flete marítimo)
    tp = tariff_params(tasa, settings={**settings, "flete_maritimo_usd": flete_maritimo})

    # Márgenes
    mn_pct = _to_decimal(margen_nuestro_pct) if margen_nuestro_pct is not None else _to_decimal(settings.get("margen_nuestro_pct", DEFAULTS["margen_nuestro_pct"]))
    mc_pct = _to_decimal(margen_cliente_pct) if margen_cliente_pct is not None else _to_decimal(settings.get("margen_cliente_pct", DEFAULTS["margen_cliente_pct"]))

    if precio_base <= ZERO:
        warnings.append("precio FOB ausente o cero — no se calcula")
        return _empty_result(country_code, tasa, entry_fraccion, tc, mn_pct, mc_pct, warnings)
    if piezas <= 0:
        warnings.append("piezas_contenedor ausente — no se calcula")
        return _empty_result(country_code, tasa, entry_fraccion, tc, mn_pct, mc_pct, warnings)

    with localcontext() as ctx:
        ctx.prec = 28
        ctx.rounding = ROUND_HALF_UP

        # Pasos 1-6: build-up en USD
        paso1 = precio_base
        paso2 = paso1 * tp["multiplicador_arancelario"]
        paso3 = paso2 * Decimal(piezas)
        paso4 = paso3 + tp["precio_contenedor_usd"]
        aranceles_total_pct = tp["tasa_arancelaria_pct"] + tp["dta_pct"]
        paso5 = paso4 * (ONE + aranceles_total_pct / HUNDRED)
        paso6 = paso5 * (ONE + tp["gastos_aduanales_pct"] / HUNDRED)

        # Paso 7: a MXN
        paso7 = paso6 * tc

        # Paso 8: flete local MXN
        flete_local = _to_decimal(settings.get("flete_local_mxn", DEFAULTS["flete_local_mxn"]))
        paso8 = paso7 + flete_local

        # Paso 9: landed cost por unidad (ANCHOR)
        paso9 = (paso8 / Decimal(piezas)).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)

        # Pasos 10-14
        paso10, paso11, paso12, paso13, paso14 = _calc_from_paso9(paso9, cp, mn_pct, mc_pct)

        return PricingResult(
            paso1=paso1, paso2=paso2, paso3=paso3, paso4=paso4, paso5=paso5,
            paso6=paso6, paso7=paso7, paso8=paso8, paso9=paso9, paso10=paso10,
            paso11=paso11, paso12=paso12, paso13=paso13, paso14=paso14,
            margen_nuestro_effective=mn_pct,
            margen_cliente_effective=mc_pct,
            fraccion_arancelaria=entry_fraccion,
            tasa_arancelaria_pct=tasa,
            tipo_cambio=tc,
            country_code=country_code,
            warnings=warnings,
        )


def compute_from_landed_cost(
    landed_cost_mxn: Decimal | float | str,
    *,
    settings: dict | None = None,
    country_code: str = "MX",
    margen_nuestro_pct: Decimal | float | str | None = None,
    margen_cliente_pct: Decimal | float | str | None = None,
) -> PricingResult:
    """Modo Landed Cost directo: el usuario provee paso 9 (landed cost MXN
    por unidad) sin recorrer pasos 1-8. Útil cuando ya tenemos la cotización
    landed de otro broker o de una compra previa.

    Devuelve PricingResult con pasos 1-8 en cero y pasos 9-14 calculados.
    """
    settings = settings or {}
    warnings: list[str] = []
    paso9 = _to_decimal(landed_cost_mxn).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)

    mn_pct = _to_decimal(margen_nuestro_pct) if margen_nuestro_pct is not None else _to_decimal(settings.get("margen_nuestro_pct", DEFAULTS["margen_nuestro_pct"]))
    mc_pct = _to_decimal(margen_cliente_pct) if margen_cliente_pct is not None else _to_decimal(settings.get("margen_cliente_pct", DEFAULTS["margen_cliente_pct"]))
    tc = _to_decimal(settings.get("tc_usd_mxn", DEFAULTS["tc_usd_mxn"]))
    cp = country_params(country_code, settings=settings)

    if paso9 <= ZERO:
        warnings.append("landed_cost_mxn ausente o cero — no se calcula")
        return _empty_result(country_code, ZERO, "—", tc, mn_pct, mc_pct, warnings)

    with localcontext() as ctx:
        ctx.prec = 28
        ctx.rounding = ROUND_HALF_UP
        paso10, paso11, paso12, paso13, paso14 = _calc_from_paso9(paso9, cp, mn_pct, mc_pct)

    return PricingResult(
        paso1=ZERO, paso2=ZERO, paso3=ZERO, paso4=ZERO, paso5=ZERO,
        paso6=ZERO, paso7=ZERO, paso8=ZERO, paso9=paso9, paso10=paso10,
        paso11=paso11, paso12=paso12, paso13=paso13, paso14=paso14,
        margen_nuestro_effective=mn_pct,
        margen_cliente_effective=mc_pct,
        fraccion_arancelaria="—",
        tasa_arancelaria_pct=ZERO,
        tipo_cambio=tc,
        country_code=country_code,
        mode="landed_cost",
        warnings=warnings,
    )


def compute_backward(
    retail_target_mxn: Decimal | float | str,
    *,
    retail_includes_iva: bool = True,
    settings: dict | None = None,
    country_code: str = "MX",
    margen_nuestro_pct: Decimal | float | str | None = None,
    margen_cliente_pct: Decimal | float | str | None = None,
    piezas_contenedor: int | None = None,
    carton_qty: int | None = None,
    carton_cbm: Decimal | float | str | None = None,
    tasa_arancelaria_pct: Decimal | float | str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
) -> PricingResult:
    """Modo Backward (HD/Amazon): el usuario provee precio retail objetivo
    en MXN, el motor invierte los 14 pasos para devolver el FOB USD máximo
    que dejaría intactos los márgenes deseados.

    `retail_includes_iva=True` (default) — retail_target_mxn = paso 13
    (público con IVA). False → paso 12 (público sin IVA).

    Si se omiten piezas (piezas_contenedor o carton_qty+carton_cbm) o el
    tariff context (tasa o cat+subcat), sólo invierte de retail a paso 9
    (landed cost MXN). En ese caso paso1..paso8 quedan en cero.
    """
    settings = settings or {}
    warnings: list[str] = []
    target = _to_decimal(retail_target_mxn)

    mn_pct = _to_decimal(margen_nuestro_pct) if margen_nuestro_pct is not None else _to_decimal(settings.get("margen_nuestro_pct", DEFAULTS["margen_nuestro_pct"]))
    mc_pct = _to_decimal(margen_cliente_pct) if margen_cliente_pct is not None else _to_decimal(settings.get("margen_cliente_pct", DEFAULTS["margen_cliente_pct"]))
    tc = _to_decimal(settings.get("tc_usd_mxn", DEFAULTS["tc_usd_mxn"]))
    cp = country_params(country_code, settings=settings)

    if target <= ZERO:
        warnings.append("retail_target_mxn ausente o cero — no se calcula")
        return _empty_result(country_code, ZERO, "—", tc, mn_pct, mc_pct, warnings)

    # Resolve tariff (needed only for the FOB-side inversion)
    if tasa_arancelaria_pct is not None:
        tasa = _to_decimal(tasa_arancelaria_pct)
        fraccion = "—"
    elif category or subcategory:
        entry = lookup_tariff(category, subcategory)
        tasa = entry.tasa_pct
        fraccion = entry.fraccion
    else:
        tasa = ZERO
        fraccion = "—"

    # Resolve piezas (override > carton_qty+carton_cbm derivation)
    piezas = piezas_contenedor or 0
    if piezas <= 0 and carton_qty and carton_cbm:
        piezas = _derive_piezas_from_cbm(
            {"carton_qty": carton_qty, "carton_cbm": carton_cbm}, settings
        )
        if piezas > 0:
            warnings.append(f"piezas_contenedor derivado de carton_qty + carton_cbm = {piezas}")

    with localcontext() as ctx:
        ctx.prec = 28
        ctx.rounding = ROUND_HALF_UP

        # Retail → paso13 → paso12 → paso11 → paso9
        if retail_includes_iva:
            paso13 = target.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            paso12 = paso13 / (ONE + cp["iva_pct"] / HUNDRED)
        else:
            paso12 = target
            paso13 = (paso12 * (ONE + cp["iva_pct"] / HUNDRED)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

        mc = mc_pct / HUNDRED
        if mc >= ONE:
            warnings.append(f"margen_cliente_pct={mc_pct}% inválido (≥100%)")
            return _empty_result(country_code, tasa, fraccion, tc, mn_pct, mc_pct, warnings)
        paso11 = paso12 * (ONE - mc)

        mn = mn_pct / HUNDRED
        total_desc = (cp["descuentos_pct"] + cp["descuentos_na_pct"] + cp["gasto_fijo_pct"]) / HUNDRED
        if mn + total_desc >= ONE:
            warnings.append(f"divisor inválido: mn={mn_pct}% + total_desc={total_desc * HUNDRED}% >= 100%")
            return _empty_result(country_code, tasa, fraccion, tc, mn_pct, mc_pct, warnings)
        # Forma corregida (Salo 2026-05-11): margen 'mn' es sobre venta, no
        # sobre costo. Inversa de paso11 = landing / (1 - mn - td):
        #   landing = paso11 * (1 - mn - td)
        paso9 = (paso11 * (ONE - mn - total_desc)).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
        paso10 = paso9 + (paso11 * total_desc)

        # paso14: corre el redondeo psicológico forward sobre paso13 inferido
        paso14 = _round_psychological(paso13, cp["psychological_prices"])

        # Inversion to FOB requires piezas + tariff context
        if piezas <= 0:
            warnings.append("piezas ausentes — sólo invertí hasta paso 9 (landed cost MXN)")
            return PricingResult(
                paso1=ZERO, paso2=ZERO, paso3=ZERO, paso4=ZERO, paso5=ZERO,
                paso6=ZERO, paso7=ZERO, paso8=ZERO, paso9=paso9, paso10=paso10,
                paso11=paso11, paso12=paso12, paso13=paso13, paso14=paso14,
                margen_nuestro_effective=mn_pct,
                margen_cliente_effective=mc_pct,
                fraccion_arancelaria=fraccion,
                tasa_arancelaria_pct=tasa,
                tipo_cambio=tc,
                country_code=country_code,
                mode="backward_partial",
                warnings=warnings,
            )

        # paso9 → paso8 → paso7 → paso6 → paso5 → paso4 → paso3 → paso2 → paso1
        flete_local = _to_decimal(settings.get("flete_local_mxn", DEFAULTS["flete_local_mxn"]))
        flete_maritimo = _to_decimal(settings.get("flete_maritimo_usd", DEFAULTS["flete_maritimo_usd"]))
        ga_pct = _to_decimal(settings.get("gastos_aduanales_pct", DEFAULTS["gastos_aduanales_pct"]))
        dta_pct = _to_decimal(settings.get("dta_pct", DEFAULTS["dta_pct"]))
        aranceles_total_pct = tasa + dta_pct
        multiplicador = _multiplier_from_tasa(tasa)

        paso8 = paso9 * Decimal(piezas)
        paso7 = paso8 - flete_local
        if tc <= ZERO:
            warnings.append("tc_usd_mxn ≤ 0 — no se invierte")
            paso1 = paso2 = paso3 = paso4 = paso5 = paso6 = ZERO
        else:
            paso6 = paso7 / tc
            paso5 = paso6 / (ONE + ga_pct / HUNDRED)
            paso4 = paso5 / (ONE + aranceles_total_pct / HUNDRED)
            paso3 = paso4 - flete_maritimo
            if paso3 <= ZERO:
                warnings.append(
                    f"infeasible: tras restar flete_maritimo (${flete_maritimo}) el "
                    f"FOB total (paso 3) sería ≤ 0 — el retail target no cubre los "
                    f"costos fijos del contenedor"
                )
                paso1 = paso2 = paso3 = ZERO
            else:
                paso2 = paso3 / Decimal(piezas)
                if multiplicador <= ZERO:
                    paso1 = ZERO
                else:
                    paso1 = paso2 / multiplicador

        return PricingResult(
            paso1=paso1.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP) if paso1 > ZERO else ZERO,
            paso2=paso2 if paso1 > ZERO else ZERO,
            paso3=paso3 if paso1 > ZERO else ZERO,
            paso4=paso4 if paso1 > ZERO else ZERO,
            paso5=paso5 if paso1 > ZERO else ZERO,
            paso6=paso6 if paso1 > ZERO else ZERO,
            paso7=paso7,
            paso8=paso8,
            paso9=paso9,
            paso10=paso10,
            paso11=paso11,
            paso12=paso12,
            paso13=paso13,
            paso14=paso14,
            margen_nuestro_effective=mn_pct,
            margen_cliente_effective=mc_pct,
            fraccion_arancelaria=fraccion,
            tasa_arancelaria_pct=tasa,
            tipo_cambio=tc,
            country_code=country_code,
            mode="backward",
            warnings=warnings,
        )


def _multiplier_from_tasa(tasa_pct: Decimal) -> Decimal:
    """Reproduce defaults._multiplier_from_rate sin importarla (evita cycle)."""
    normalized = tasa_pct.quantize(TWO_PLACES)
    if normalized == Decimal("0.00"):
        return Decimal("1.15")
    if normalized == Decimal("5.00"):
        return Decimal("1.10")
    return Decimal("1.00")


def _calc_from_paso9(
    paso9: Decimal,
    cp: dict,
    mn_pct: Decimal,
    mc_pct: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    """Calcula pasos 10-14 desde paso9 (landed cost unitario MXN).

    Formula corregida (Salo 2026-05-11): el margen Lloyds 'mn' es margen
    real sobre venta (utilidad/venta), no margen sobre costo.

    Cadena:
      paso11 = landing / (1 - mn - total_desc)
        => venta tal que (venta - landing - venta*td) / venta = mn
      paso10 = landing + paso11 * total_desc   (costo total real)
      paso12 = paso11 / (1 - mc)               (publico sin IVA)
      paso13 = paso12 * (1 + iva)              (publico con IVA)

    Si mn + total_desc >= 1, el divisor seria <=0 (margen imposible).
    """
    mn = mn_pct / HUNDRED
    desc = cp["descuentos_pct"] / HUNDRED
    desc_na = cp["descuentos_na_pct"] / HUNDRED
    gf = cp["gasto_fijo_pct"] / HUNDRED
    total_desc = desc + desc_na + gf

    venta_divisor = ONE - mn - total_desc
    if venta_divisor <= ZERO:
        raise ValueError(
            f"Divisor invalido: margen_nuestro={mn_pct}% + total_desc={total_desc * HUNDRED}% "
            f">= 100%. No se puede alcanzar ese margen con esos gastos."
        )

    paso11 = paso9 / venta_divisor
    paso10 = paso9 + (paso11 * total_desc)

    mc = mc_pct / HUNDRED
    if (ONE - mc) <= ZERO:
        raise ValueError(f"Margen cliente >= 100%: {mc_pct}%")
    paso12 = paso11 / (ONE - mc)

    paso13 = (paso12 * (ONE + cp["iva_pct"] / HUNDRED)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    paso14 = _round_psychological(paso13, cp["psychological_prices"])

    return paso10, paso11, paso12, paso13, paso14


def _round_psychological(price: Decimal, tiers: list[Decimal]) -> Decimal:
    if not tiers:
        return price

    max_tier = max(tiers)
    base_unit = Decimal("100") if max_tier >= Decimal("10") else Decimal("10")
    base = (price / base_unit).to_integral_value(rounding=ROUND_DOWN) * base_unit
    remainder = price - base

    nearest = tiers[0]
    min_distance = abs(remainder - tiers[0])
    for tier in tiers[1:]:
        distance = abs(remainder - tier)
        if distance < min_distance:
            min_distance = distance
            nearest = tier

    result = base + nearest
    if result < price - base_unit / Decimal("2"):
        result = base + base_unit + tiers[0]
    return result


def _derive_piezas_from_cbm(row: dict, settings: dict) -> int:
    """Derive piezas_contenedor from carton_qty + carton_cbm.

    Fórmula del usuario: piezas = floor((cbm_40hq / carton_cbm) * pcs_por_caja).
    cbm_40hq default 67 (40HQ útil), editable vía settings.
    """
    qty_raw = row.get("carton_qty")
    cbm_raw = row.get("carton_cbm")
    if qty_raw in (None, "", 0, "0") or cbm_raw in (None, "", 0, "0"):
        return 0
    try:
        qty = Decimal(str(qty_raw))
        cbm = Decimal(str(cbm_raw))
    except Exception:
        return 0
    if qty <= ZERO or cbm <= ZERO:
        return 0
    cbm_40hq = _to_decimal(settings.get("cbm_40hq", DEFAULTS["cbm_40hq"]))
    if cbm_40hq <= ZERO:
        return 0
    cartons_per_container = (cbm_40hq / cbm).to_integral_value(rounding=ROUND_DOWN)
    total = (cartons_per_container * qty).to_integral_value(rounding=ROUND_DOWN)
    return int(total)


def _to_decimal(value) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _empty_result(
    country_code: str,
    tasa: Decimal,
    fraccion: str,
    tc: Decimal,
    mn_pct: Decimal,
    mc_pct: Decimal,
    warnings: list[str],
) -> PricingResult:
    return PricingResult(
        paso1=ZERO, paso2=ZERO, paso3=ZERO, paso4=ZERO, paso5=ZERO,
        paso6=ZERO, paso7=ZERO, paso8=ZERO, paso9=ZERO, paso10=ZERO,
        paso11=ZERO, paso12=ZERO, paso13=ZERO, paso14=ZERO,
        margen_nuestro_effective=mn_pct,
        margen_cliente_effective=mc_pct,
        fraccion_arancelaria=fraccion,
        tasa_arancelaria_pct=tasa,
        tipo_cambio=tc,
        country_code=country_code,
        warnings=warnings,
    )
