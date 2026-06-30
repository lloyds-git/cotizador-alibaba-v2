"""
Busqueda de competencia en Amazon MX, Mercado Libre MX y Petco MX usando la
herramienta nativa `web_search` de Claude.

Flujo: se arma una query a partir de los datos del producto (descripcion,
medidas, material), Claude busca acotado a los dominios de DOMINIOS,
lee las paginas y devuelve un JSON con los listings que mas se parecen
(titulo, precio MXN, rating, reseñas, vendedor, url, confianza de match).

NO persiste nada: devuelve candidatos. El usuario confirma cuales coinciden y
la ruta los guarda en la tabla competidor_listings.

Costo aprox por busqueda: ~$0.01-0.03 USD (web_search ~$10/1000 + tokens).
"""

from __future__ import annotations

import json
import os
import re

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODELO_DEFAULT = os.getenv("COMPETENCIA_MODELO", "claude-sonnet-4-6")
# Version de la server-tool de web search. 20250305 es estable; se puede subir
# a 20260209 via env si la API lo acepta.
WEBSEARCH_TOOL = os.getenv("COMPETENCIA_WEBSEARCH_TOOL", "web_search_20250305")
MAX_BUSQUEDAS = int(os.getenv("COMPETENCIA_MAX_BUSQUEDAS", "9"))

# marketplace -> dominio para allowed_domains
DOMINIOS = {
    "amazon_mx": "amazon.com.mx",
    "mercadolibre_mx": "mercadolibre.com.mx",
    "petco_mx": "petco.com.mx",
}

# dominio -> nombre legible para el prompt
NOMBRES = {
    "amazon.com.mx": "Amazon Mexico",
    "mercadolibre.com.mx": "Mercado Libre Mexico",
    "petco.com.mx": "Petco Mexico",
}


def construir_query(producto) -> str:
    """Arma una consulta de busqueda a partir de un Producto (o dict)."""
    def _get(k):
        if isinstance(producto, dict):
            return producto.get(k)
        return getattr(producto, k, None)

    partes = [_get("descripcion"), _get("material"), _get("medidas")]
    return " ".join(p.strip() for p in partes if p and str(p).strip())


def _extraer_json(texto: str) -> list:
    """Extrae el primer arreglo JSON del texto de Claude.

    Tolera bloques ```json ... ``` y texto alrededor del arreglo.
    Devuelve [] si no logra parsear.
    """
    if not texto:
        return []
    # 1) bloque ```json ... ``` o ``` ... ```
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", texto, re.DOTALL)
    cand = m.group(1) if m else None
    # 2) primer '[' hasta el ultimo ']'
    if cand is None:
        ini = texto.find("[")
        fin = texto.rfind("]")
        if ini != -1 and fin != -1 and fin > ini:
            cand = texto[ini:fin + 1]
    if cand is None:
        return []
    try:
        data = json.loads(cand)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _texto_de_respuesta(response) -> str:
    """Concatena los bloques de texto de la respuesta (ignora tool_use)."""
    out = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            out.append(block.text)
    return "\n".join(out)


def _normalizar(item: dict, marketplaces: list[str]) -> dict | None:
    """Valida/normaliza un candidato devuelto por Claude. None si invalido."""
    mp = (item.get("marketplace") or "").strip().lower()
    if mp in ("amazon", "amazon_mx", "amazon mx"):
        mp = "amazon_mx"
    elif mp in ("mercadolibre", "mercado_libre", "mercadolibre_mx", "meli", "ml"):
        mp = "mercadolibre_mx"
    elif mp in ("petco", "petco_mx", "petco mx", "petco.com.mx"):
        mp = "petco_mx"
    if mp not in DOMINIOS:
        return None
    titulo = (item.get("titulo") or item.get("title") or "").strip()
    url = (item.get("url") or "").strip()
    if not titulo or not url:
        return None

    def _num(v):
        if v is None or v == "":
            return None
        try:
            return float(str(v).replace("$", "").replace(",", "").strip())
        except (ValueError, TypeError):
            return None

    return {
        "marketplace": mp,
        "titulo": titulo,
        "precio_mxn": _num(item.get("precio_mxn") or item.get("precio") or item.get("price")),
        "rating": _num(item.get("rating")),
        "num_reviews": int(_num(item.get("num_reviews") or item.get("reviews")) or 0) or None,
        "vendedor": (item.get("vendedor") or item.get("seller") or None),
        "url": url,
        "imagen_url": (item.get("imagen_url") or item.get("imagen") or item.get("image") or None),
        "confianza_match": _num(item.get("confianza_match") or item.get("confianza")),
    }


def buscar_candidatos(query: str, marketplaces: list[str] | None = None) -> dict:
    """Busca candidatos de competencia. NO persiste.

    Devuelve: {ok, candidatos, query, modelo, error}
    """
    query = (query or "").strip()
    if not query:
        return {"ok": False, "candidatos": [], "query": query,
                "modelo": MODELO_DEFAULT, "error": "Query vacia."}

    if not os.getenv("ANTHROPIC_API_KEY"):
        return {"ok": False, "candidatos": [], "query": query,
                "modelo": MODELO_DEFAULT,
                "error": "Falta ANTHROPIC_API_KEY en el entorno."}

    marketplaces = marketplaces or list(DOMINIOS.keys())
    dominios = [DOMINIOS[m] for m in marketplaces if m in DOMINIOS]
    if not dominios:
        dominios = list(DOMINIOS.values())

    nombres = ", ".join(NOMBRES.get(d, d) for d in dominios)

    prompt = (
        f"Busca en {nombres} productos que correspondan a este articulo:\n\n"
        f'"{query}"\n\n'
        "Para CADA marketplace encuentra hasta 4 listings que sean el mismo "
        "producto o el equivalente mas parecido. Usa los resultados reales de "
        "busqueda (no inventes URLs ni precios).\n\n"
        "EL PRECIO ES OBLIGATORIO. En los resultados de busqueda de estos "
        "sitios el precio casi SIEMPRE aparece como "
        "texto junto al producto (ej. '$359.00', '$1,299'). Lee y extrae el "
        "precio exacto de CADA listing tal cual aparece en los resultados. "
        "Si en un resultado no alcanzaste a ver el precio, haz una busqueda "
        "adicional mas especifica (por el nombre del producto, o agregando "
        "'precio') hasta encontrarlo. Solo deja precio_mxn en null si "
        "despues de intentarlo de plano no aparece ningun precio; NUNCA "
        "pongas 0 ni un precio inventado.\n\n"
        "Responde UNICAMENTE con un bloque JSON (sin texto antes ni despues) "
        "que sea un arreglo de objetos con esta forma exacta:\n"
        "```json\n"
        "[\n"
        "  {\n"
        '    "marketplace": "amazon_mx" | "mercadolibre_mx" | "petco_mx",\n'
        '    "titulo": "titulo del listing",\n'
        '    "precio_mxn": 359.0,\n'
        '    "rating": 4.5,\n'
        '    "num_reviews": 123,\n'
        '    "vendedor": "nombre del vendedor o null",\n'
        '    "url": "https://...",\n'
        '    "imagen_url": "https://... o null",\n'
        '    "confianza_match": 0.85\n'
        "  }\n"
        "]\n"
        "```\n"
        "precio_mxn es el precio al publico en pesos mexicanos (numero, sin "
        "simbolos ni comas; usa el precio actual/con descuento si hay oferta). "
        "confianza_match es 0.0-1.0 segun que tan seguro es que sea el mismo "
        "producto. Si no encuentras nada, responde []."
    )

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=MODELO_DEFAULT,
            max_tokens=4096,
            tools=[{
                "type": WEBSEARCH_TOOL,
                "name": "web_search",
                "max_uses": MAX_BUSQUEDAS,
                "allowed_domains": dominios,
            }],
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.RateLimitError:
        return {"ok": False, "candidatos": [], "query": query,
                "modelo": MODELO_DEFAULT,
                "error": ("Límite de tasa de la API alcanzado (busquedas web "
                          "consumen muchos tokens). Espera ~1 minuto y reintenta.")}
    except anthropic.APIError as e:
        return {"ok": False, "candidatos": [], "query": query,
                "modelo": MODELO_DEFAULT, "error": f"Error de la API: {e}"}

    texto = _texto_de_respuesta(response)
    crudos = _extraer_json(texto)
    candidatos = [c for c in (_normalizar(it, marketplaces) for it in crudos) if c]

    return {
        "ok": True,
        "candidatos": candidatos,
        "query": query,
        "modelo": MODELO_DEFAULT,
        "error": None if candidatos else "Sin coincidencias claras.",
    }
