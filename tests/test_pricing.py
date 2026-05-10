from app.pricing import (
    calcular_landed_mxn,
    calcular_retail_mxn,
    calcular_margen_hd,
)


def test_landed_mxn_basico():
    r = calcular_landed_mxn(fob_usd=10.0, tipo_cambio=20.0, factor_importacion=1.4)
    assert r == 280.0


def test_landed_mxn_redondeado():
    r = calcular_landed_mxn(fob_usd=8.0, tipo_cambio=20.5, factor_importacion=1.35)
    assert r == round(8.0 * 20.5 * 1.35, 2)


def test_retail_sugerido():
    r = calcular_retail_mxn(landed_mxn=280.0, margen_objetivo=0.60, iva=0.16)
    assert r == 812.0


def test_margen_hd():
    m = calcular_margen_hd(costo=280.0, retail_con_iva=812.0, iva=0.16)
    assert round(m, 4) == 0.6


def test_pricing_completo_para_producto():
    """Producto con FOB 12.5 USD -> debe dar precios coherentes."""
    landed = calcular_landed_mxn(12.5, 20.0, 1.4)
    retail = calcular_retail_mxn(landed, 0.40, 0.16)
    margen = calcular_margen_hd(landed, retail, 0.16)
    assert round(margen, 2) == 0.40
