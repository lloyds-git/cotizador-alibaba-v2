"""Gate de calidad para la ingestion de productos.

Funciones compartidas entre el CLI (app/cli.py) y el pipeline web
(app/pdf_pipeline.py). Antes vivian solo en cli.py; se extrajeron aqui para
que el endpoint POST /api/ingest/pdf pueda aplicar el mismo veredicto sin
importar cli.
"""

# Palabras/patrones que delatan filas basura (headers/footers del PDF)
SOSPECHOSA_KEYWORDS = (
    "quotation", "tel:", "adress:", "address:", "total cost",
    "no. model", "bio-tec", "@sina.com", "@alibaba",
)


def es_sospechosa(p) -> str | None:
    """Devuelve motivo si el producto parece basura; None si parece OK."""
    sku = (p.sku or "").strip()
    desc = (p.descripcion or "").strip()
    desc_low = desc.lower()
    if not sku or sku.startswith("AUTO-"):
        return "SKU autogenerado (parser no detecto codigo real)"
    if "alibaba.com/product-detail" in desc_low:
        return "descripcion es URL de Alibaba"
    for k in SOSPECHOSA_KEYWORDS:
        if k in desc_low:
            return f"descripcion contiene '{k}' (header/footer del PDF)"
    if len(desc) < 10 and not (p.material and len(p.material.strip()) >= 3):
        return f"descripcion muy corta ({len(desc)} chars) y sin material"
    if not p.fob_usd or p.fob_usd <= 0:
        return "FOB ausente o <= 0"
    return None


def calcular_veredicto(prods) -> dict:
    """Analiza una lista de productos y devuelve metricas + veredicto.

    Veredicto:
      - "OK"          si SKUs reales >= 80% y sospechosas <= 20%
      - "REINGESTAR"  si SKUs reales < 50%
      - "REVISAR"     zona gris en el medio
    """
    n = len(prods)
    if n == 0:
        return {
            "n": 0,
            "veredicto": "VACIO",
            "motivo": "No se extrajo ningun producto del PDF",
            "cobertura": {},
            "sospechosas": [],
        }

    def vacio(v):
        return v is None or (isinstance(v, str) and not v.strip())

    campos = [
        ("SKU real (no AUTO-)", lambda p: p.sku and not p.sku.startswith("AUTO-")),
        ("FOB > 0", lambda p: p.fob_usd and p.fob_usd > 0),
        ("material", lambda p: not vacio(p.material)),
        ("medidas", lambda p: not vacio(p.medidas)),
        ("peso_kg", lambda p: p.peso_kg is not None),
        ("color", lambda p: not vacio(p.color)),
        ("moq", lambda p: not vacio(p.moq)),
        ("packing", lambda p: not vacio(p.packing)),
        ("cbm", lambda p: p.cbm is not None),
        ("lead_time", lambda p: not vacio(p.lead_time)),
        ("foto >= 1", lambda p: len(p.fotos) >= 1),
    ]
    cobertura = {nombre: sum(1 for p in prods if fn(p)) for nombre, fn in campos}
    sospechosas = [(p, es_sospechosa(p)) for p in prods]
    sospechosas = [(p, m) for p, m in sospechosas if m]

    pct_sku = cobertura["SKU real (no AUTO-)"] / n
    pct_susp = len(sospechosas) / n

    if pct_sku >= 0.80 and pct_susp <= 0.20:
        veredicto = "OK"
        motivo = "SKUs reales >= 80%, sospechosas <= 20%"
    elif pct_sku < 0.50:
        veredicto = "REINGESTAR"
        motivo = f"Solo {pct_sku:.0%} de SKUs reales (umbral 50%)"
    else:
        veredicto = "REVISAR"
        motivo = f"SKUs reales {pct_sku:.0%}, sospechosas {pct_susp:.0%}"

    return {
        "n": n,
        "veredicto": veredicto,
        "motivo": motivo,
        "cobertura": cobertura,
        "sospechosas": sospechosas,
    }
