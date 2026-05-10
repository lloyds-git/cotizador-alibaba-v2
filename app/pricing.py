"""
Logica de calculo de precios para productos importados.

Flujo: FOB USD -> Landed MXN -> Retail MXN -> Margen HD

Parametros tipicos:
- factor_importacion: 1.30 a 1.45 (incluye flete, aduana, impuestos, manejo)
- margen_objetivo: 0.35 a 0.65 segun categoria
- IVA Mexico: 0.16
"""

from __future__ import annotations


def calcular_landed_mxn(
    fob_usd: float,
    tipo_cambio: float,
    factor_importacion: float,
) -> float:
    """
    Calcula el costo landed en pesos mexicanos.

    fob_usd * tipo_cambio * factor_importacion
    """
    return round(fob_usd * tipo_cambio * factor_importacion, 2)


def calcular_retail_mxn(
    landed_mxn: float,
    margen_objetivo: float,
    iva: float = 0.16,
) -> float:
    """
    Calcula el precio retail (con IVA) para alcanzar un margen objetivo.

    margen = 1 - costo/(retail_sin_iva)
    retail_sin_iva = costo / (1 - margen)
    retail_con_iva = retail_sin_iva * (1 + iva)
    """
    if margen_objetivo >= 1.0 or margen_objetivo < 0:
        raise ValueError("margen_objetivo debe estar entre 0 y <1")
    retail_sin_iva = landed_mxn / (1 - margen_objetivo)
    retail_con_iva = retail_sin_iva * (1 + iva)
    return round(retail_con_iva, 2)


def calcular_margen_hd(
    costo: float,
    retail_con_iva: float,
    iva: float = 0.16,
) -> float:
    """
    Calcula el margen efectivo dado un costo y un retail con IVA.
    """
    if retail_con_iva <= 0:
        return 0.0
    retail_sin_iva = retail_con_iva / (1 + iva)
    return 1 - costo / retail_sin_iva
