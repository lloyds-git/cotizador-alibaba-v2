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

MODELO_DEFAULT = os.getenv("COMPETENCIA_MODELO", "claude-haiku-4-5")
# web_search_20260209 (filtrado dinamico) solo en modelos recientes; Haiku 4.5 da
# 400 -> usa la basica. Se autoselecciona segun el modelo; override con COMPETENCIA_WEBSEARCH_TOOL.
_WS_DINAMICO = ("opus-4-6", "opus-4-7", "opus-4-8", "sonnet-5", "sonnet-4-6")
WEBSEARCH_TOOL = os.getenv("COMPETENCIA_WEBSEARCH_TOOL") or (
    "web_search_20260209" if any(m in MODELO_DEFAULT for m in _WS_DINAMICO)
    else "web_search_20250305"
)
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

# dominio canonico -> clave de marketplace (inverso de DOMINIOS)
DOMINIO_A_KEY = {d: k for k, d in DOMINIOS.items()}


def _host_de_url(url: str) -> str:
    """Host de una URL o dominio, en minusculas y sin 'www.'. '' si no aplica."""
    s = (url or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"^[a-z]+://", "", s)           # quitar esquema http(s)://
    s = s.split("/")[0].split("?")[0].split("#")[0]  # cortar path/query/fragment
    s = s.split("@")[-1].split(":")[0]         # quitar credenciales y puerto
    if s.startswith("www."):
        s = s[4:]
    return s


def _key_de_dominio(host: str) -> str:
    """Clave de marketplace del host: canonica para los 3 sitios; el dominio si no."""
    for dom, key in DOMINIO_A_KEY.items():
        if host == dom or host.endswith("." + dom):
            return key
    return host


def resolver_dominios(sitios_categoria: str | None = None,
                      extra: str | None = None) -> list[str]:
    """Lista final de dominios donde buscar (CSV de la categoria + extra, se suman).

    Acepta dominios sueltos o URLs completas (extrae el host). Dedup preservando
    orden. Si queda vacio, cae a los 3 dominios default (DOMINIOS).
    """
    dominios: list[str] = []
    for fuente in (sitios_categoria, extra):
        if not fuente:
            continue
        for token in re.split(r"[,\s;]+", fuente):
            host = _host_de_url(token)
            if host and host not in dominios:
                dominios.append(host)
    return dominios or list(DOMINIOS.values())


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


def _conto_busquedas(response) -> int:
    """Cuantas veces el modelo ejecuto web_search (0 = no busco / rechazo)."""
    return sum(
        1 for b in response.content
        if getattr(b, "type", None) == "server_tool_use"
    )


def _normalizar(item: dict, dominios: list[str]) -> dict | None:
    """Valida/normaliza un candidato devuelto por Claude. None si invalido.

    El marketplace se deriva del host de la URL (fuente de verdad): clave
    canonica para los 3 sitios ('amazon_mx', ...) y el dominio para sitios extra.
    Descarta candidatos cuyo host no este entre los `dominios` buscados.
    """
    titulo = (item.get("titulo") or item.get("title") or "").strip()
    url = (item.get("url") or "").strip()
    if not titulo or not url:
        return None

    host = _host_de_url(url)
    if not any(host == d or host.endswith("." + d) for d in dominios):
        return None
    mp = _key_de_dominio(host)

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


def buscar_candidatos(query: str, dominios: list[str] | None = None) -> dict:
    """Busca candidatos de competencia. NO persiste.

    `dominios` es la lista ya resuelta de dominios donde buscar (ver
    resolver_dominios). Si es None/vacia usa los 3 default (DOMINIOS).

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

    dominios = [d for d in (dominios or []) if d] or list(DOMINIOS.values())

    nombres = ", ".join(NOMBRES.get(d, d) for d in dominios)

    prompt = (
        "Tienes habilitada la herramienta web_search y DEBES usarla. "
        "Ejecuta busquedas reales; NO respondas que no puedes buscar, que no "
        "tienes acceso a los catalogos, ni que no hay datos en tiempo real: "
        "esas respuestas son incorrectas. Simplemente usa web_search.\n\n"
        f"Busca en {nombres} productos que correspondan a este articulo:\n\n"
        f'"{query}"\n\n'
        "Para CADA sitio devuelve hasta 4 listings del MISMO TIPO de producto. "
        "IMPORTANTE: casi nunca existe el producto identico; el objetivo es "
        "comparar precios de mercado, asi que INCLUYE los listings mas parecidos "
        "que encuentres aunque difieran en tamano, marca, color o variante "
        "(p. ej. otros ramos artificiales de rosas). NO los descartes por no ser "
        "identicos; en vez de eso baja su confianza_match. Usa los resultados "
        "reales de busqueda (no inventes URLs ni precios).\n\n"
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
        '    "marketplace": "amazon_mx | mercadolibre_mx | petco_mx | el dominio del sitio",\n'
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
        "confianza_match es 0.0-1.0 segun que tan parecido es al articulo "
        "buscado (1.0 = practicamente el mismo; 0.4-0.7 = mismo tipo pero "
        "distinta variante/tamano/marca). Devuelve [] SOLO si de plano no hay "
        "ningun producto del mismo tipo en ese sitio."
    )

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=MODELO_DEFAULT,
            max_tokens=4096,
            # thinking off: predecible en costo/latencia y compatible con Sonnet 5
            # y Haiku 4.5 (ambos aceptan disabled).
            thinking={"type": "disabled"},
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
    candidatos = [c for c in (_normalizar(it, dominios) for it in crudos) if c]
    busquedas = _conto_busquedas(response)

    if candidatos:
        error = None
    elif busquedas == 0:
        # El modelo no invoco web_search (tipico de Haiku: contesta "no puedo
        # buscar"). No es que no haya competencia; es el modelo. Da una pista util.
        error = (f"El modelo '{MODELO_DEFAULT}' no ejecuto ninguna busqueda web. "
                 "Usa un modelo mas capaz: define COMPETENCIA_MODELO=claude-sonnet-5 "
                 "en el .env y reinicia.")
    else:
        error = "Sin coincidencias claras."

    return {
        "ok": True,
        "candidatos": candidatos,
        "query": query,
        "modelo": MODELO_DEFAULT,
        "error": error,
    }
