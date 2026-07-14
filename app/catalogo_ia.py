"""Bootstrapping de catalogo asistido por IA (por proyecto).

A partir de las descripciones de los productos ya ingestados de un proyecto,
Claude:
  1. proponer_categorias(): infiere el DOMINIO del catalogo y propone categorias
     + keywords (substrings que realmente aparecen en las descripciones).
  2. investigar_aranceles(): con la herramienta nativa `web_search`, investiga la
     fraccion arancelaria TIGIE (NNNN.NN.NN) y la tasa IGI% de cada categoria en
     fuentes oficiales mexicanas.

proponer_catalogo() orquesta ambas fases y devuelve una PROPUESTA transitoria
(no persiste nada): la ruta la muestra al usuario, que revisa/edita y confirma.
Al confirmar (POST /api/catalogo/aplicar-propuesta) se escriben las categorias y,
si la fraccion es valida, `arancel_estado='confirmado'` (recien ahi afecta la
cotizacion). Espejo del patron de app/competencia.py.

Costo aprox: fase 1 ~$0.01 (sin web); fase 2 ~$0.02-0.10 (web_search ~$10/1000
usos + tokens), acotado por CATALOGO_MAX_BUSQUEDAS.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import unicodedata
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

# Dos modelos distintos: categorias es inferencia de patrones (Haiku 4.5 alcanza
# y es barato); aranceles necesita razonamiento y web_search fiable para sacar la
# fraccion TIGIE correcta de fuentes oficiales (Sonnet 5). Ambos por env.
# (CATALOGO_IA_MODELO se respeta como fallback para categorias por compatibilidad.)
MODELO_CATEGORIAS = os.getenv("CATALOGO_CATEGORIAS_MODELO") or os.getenv(
    "CATALOGO_IA_MODELO", "claude-haiku-4-5"
)
MODELO_ARANCELES = os.getenv("CATALOGO_ARANCELES_MODELO", "claude-sonnet-5")
# La server-tool web_search_20260209 (filtrado dinamico, mejor precision) solo la
# soportan modelos recientes (opus 4.6+, sonnet 5 / 4.6). Haiku 4.5 y otros dan 400
# con ella -> basica 20250305. Solo aranceles usa web_search, asi que se deriva del
# modelo de aranceles; override con CATALOGO_WEBSEARCH_TOOL.
_WS_DINAMICO = ("opus-4-6", "opus-4-7", "opus-4-8", "sonnet-5", "sonnet-4-6")
WEBSEARCH_TOOL = os.getenv("CATALOGO_WEBSEARCH_TOOL") or (
    "web_search_20260209" if any(m in MODELO_ARANCELES for m in _WS_DINAMICO)
    else "web_search_20250305"
)
MAX_BUSQUEDAS = int(os.getenv("CATALOGO_MAX_BUSQUEDAS", "18"))
# Cuantas categorias investigar por llamada web_search. Con muchas categorias en
# una sola respuesta, el JSON final se trunca (excede max_tokens); por eso se
# investiga en lotes pequenos y se unen los resultados.
ARANCEL_BATCH = int(os.getenv("CATALOGO_ARANCEL_BATCH", "8"))
# Cap de descripciones enviadas al modelo (dedup + muestreo). Cientos de
# productos entran de sobra en el contexto, pero acotamos costo/latencia.
MAX_DESCRIPCIONES = int(os.getenv("CATALOGO_MAX_DESCRIPCIONES", "300"))
MAX_CHARS_DESC = 140

# Fuentes que preferimos para la investigacion arancelaria (opcional; vacio =
# sin restriccion de dominios para no limitar el recall del buscador).
_dom_env = os.getenv("CATALOGO_WEBSEARCH_DOMINIOS", "").strip()
DOMINIOS_TIGIE = [d.strip() for d in _dom_env.split(",") if d.strip()] if _dom_env else []

FRACCION_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")

# Visión: describir productos desde su foto cuando el PDF no trae descripcion
# textual (catalogos por SKU + foto + medidas, ej. proveedores tipo Tianjin).
VISION_MODELO = os.getenv("CATALOGO_VISION_MODELO", "claude-haiku-4-5")
VISION_BATCH = int(os.getenv("CATALOGO_VISION_BATCH", "6"))
VISION_MAX_SIDE = int(os.getenv("CATALOGO_VISION_MAX_SIDE", "512"))
_MEDIA_POR_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _texto_de_respuesta(response) -> str:
    """Concatena los bloques de texto de la respuesta (ignora tool_use)."""
    out = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            out.append(block.text)
    return "\n".join(out)


def _extraer_json(texto: str):
    """Extrae el primer bloque JSON (objeto {...} o arreglo [...]) del texto.

    Tolera bloques ```json ... ``` y texto alrededor. Devuelve dict/list, o None.
    """
    if not texto:
        return None
    # 1) bloque ```json ... ``` o ``` ... ```
    m = re.search(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", texto, re.DOTALL)
    cand = m.group(1) if m else None
    # 2) primer delimitador de apertura hasta el ultimo de cierre coincidente
    if cand is None:
        inicios = [i for i in (texto.find("{"), texto.find("[")) if i != -1]
        if inicios:
            ini = min(inicios)
            cierre = "}" if texto[ini] == "{" else "]"
            fin = texto.rfind(cierre)
            if fin > ini:
                cand = texto[ini:fin + 1]
    if cand is None:
        return None
    try:
        return json.loads(cand)
    except json.JSONDecodeError:
        return None


def _slugify(texto: str) -> str:
    """Normaliza a slug kebab-case ascii: minusculas, sin acentos, [a-z0-9-]."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    t = t.lower().strip()
    t = re.sub(r"[\s_]+", "-", t)
    t = re.sub(r"[^a-z0-9-]", "", t)
    t = re.sub(r"-+", "-", t).strip("-")
    return t


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace("%", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _muestrear_descripciones(descripciones: list[str]) -> tuple[list[str], int]:
    """Dedup + truncado + muestreo representativo. Devuelve (muestra, total_unicas)."""
    vistas = set()
    limpias = []
    for d in descripciones:
        if not d:
            continue
        s = str(d).strip()
        if len(s) < 5:
            continue
        clave = s.lower()
        if clave in vistas:
            continue
        vistas.add(clave)
        limpias.append(s[:MAX_CHARS_DESC])

    total = len(limpias)
    if total <= MAX_DESCRIPCIONES:
        return limpias, total

    # Muestreo representativo: ordenar (agrupa prefijos similares) y tomar un
    # paso uniforme para cubrir variedad en vez de las primeras N.
    limpias.sort(key=str.lower)
    paso = total / MAX_DESCRIPCIONES
    muestra = [limpias[int(i * paso)] for i in range(MAX_DESCRIPCIONES)]
    return muestra, total


# ---------------------------------------------------------------------------
# Fase 1: proponer categorias + keywords (sin web)
# ---------------------------------------------------------------------------

def proponer_categorias(descripciones: list[str], categorias_existentes: list[dict] | None = None) -> dict:
    """Infiere dominio y propone categorias con keywords. NO usa web_search.

    categorias_existentes (opcional): [{slug, keywords[]}] del catalogo actual del
    proyecto. Se le pide al modelo REUTILIZAR esos slugs (extendiendo sus keywords)
    en vez de inventar slugs solapados -> evita duplicar categorias al re-proponer.

    Devuelve: {ok, error, dominio, modelo, categorias:[{slug,nombre,orden,keywords[]}]}
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {"ok": False, "error": "Falta ANTHROPIC_API_KEY en el entorno.",
                "dominio": None, "modelo": MODELO_CATEGORIAS, "categorias": []}

    muestra, total = _muestrear_descripciones(descripciones)
    if not muestra:
        return {"ok": False, "error": "No hay descripciones de productos para analizar.",
                "dominio": None, "modelo": MODELO_CATEGORIAS, "categorias": []}

    listado = "\n".join(f"- {d}" for d in muestra)
    nota_muestra = (
        f"(muestra representativa de {len(muestra)} de {total} productos unicos)"
        if total > len(muestra) else f"({total} productos)"
    )

    existentes_txt = ""
    if categorias_existentes:
        lineas = []
        for c in categorias_existentes:
            kws = ", ".join((c.get("keywords") or [])[:6])
            lineas.append(f"- {c['slug']}" + (f" (keywords: {kws})" if kws else ""))
        existentes_txt = (
            "\n\nEl catalogo YA TIENE estas categorias. REUTILIZA su `slug` EXACTO "
            "cuando un producto encaje (puedes AGREGAR keywords que falten para "
            "cubrir mas productos); NO inventes un slug nuevo para un tipo que ya "
            "tiene categoria (evita duplicados como 'ramos-artificiales' junto a "
            "'ramos-rosas'). Crea categorias NUEVAS solo para tipos que estas NO "
            "cubran. Incluye en tu respuesta tanto las reutilizadas como las nuevas:\n"
            + "\n".join(lineas)
        )

    prompt = (
        "Eres un experto en catalogacion de productos importados de proveedores "
        "chinos (Alibaba) para un importador mexicano. A continuacion hay "
        f"descripciones de productos de un proyecto {nota_muestra}:\n\n"
        f"{listado}"
        f"{existentes_txt}\n\n"
        "Infiere el DOMINIO del catalogo y propon las CATEGORIAS que "
        "cubran estos productos. Para cada categoria:\n"
        "- `slug`: kebab-case en minusculas, sin acentos ni espacios (ej. "
        "'termos', 'sartenes', 'casa-jaula').\n"
        "- `nombre`: nombre legible en espanol.\n"
        "- `orden`: entero; MENOR = mayor prioridad. Las categorias mas "
        "especificas van con orden menor y las genericas con orden mayor (van al "
        "final), porque el clasificador asigna la primera keyword que coincide.\n"
        "- `keywords`: lista de substrings en minusculas (case-insensitive, NO "
        "regex) que REALMENTE aparezcan en las descripciones de arriba y que "
        "identifiquen la categoria. Incluye variantes en ingles y espanol. Las "
        "keywords de una categoria NO deben solaparse con las de otra mas "
        "generica (evita que 'bowl' caiga en dos lados).\n\n"
        "Responde UNICAMENTE con un bloque JSON (sin texto antes ni despues) con "
        "esta forma exacta:\n"
        "```json\n"
        "{\n"
        '  "dominio": "cocina",\n'
        '  "categorias": [\n'
        '    {"slug": "termos", "nombre": "Termos", "orden": 30, '
        '"keywords": ["thermos", "vacuum flask", "termo"]}\n'
        "  ]\n"
        "}\n"
        "```"
    )

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=MODELO_CATEGORIAS,
            max_tokens=4096,
            # En Sonnet 5 el thinking adaptativo viene activo por defecto; lo
            # desactivamos para mantener latencia/costo predecibles y que el
            # thinking no consuma el presupuesto de max_tokens del JSON.
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.RateLimitError:
        return {"ok": False, "modelo": MODELO_CATEGORIAS, "dominio": None, "categorias": [],
                "error": "Limite de tasa de la API alcanzado. Espera ~1 minuto y reintenta."}
    except anthropic.APIError as e:
        return {"ok": False, "modelo": MODELO_CATEGORIAS, "dominio": None, "categorias": [],
                "error": f"Error de la API: {e}"}

    data = _extraer_json(_texto_de_respuesta(response))
    if not isinstance(data, dict):
        return {"ok": False, "modelo": MODELO_CATEGORIAS, "dominio": None, "categorias": [],
                "error": "La IA no devolvio un JSON valido de categorias."}

    categorias = []
    vistos = set()
    for c in data.get("categorias", []):
        if not isinstance(c, dict):
            continue
        slug = _slugify(c.get("slug") or c.get("nombre") or "")
        if not slug or slug == "_descartar" or slug in vistos:
            continue
        vistos.add(slug)
        kws, kws_vistas = [], set()
        for kw in c.get("keywords", []) or []:
            k = (str(kw) or "").strip().lower()
            if k and k not in kws_vistas:
                kws_vistas.add(k)
                kws.append(k)
        orden = c.get("orden")
        categorias.append({
            "slug": slug,
            "nombre": (c.get("nombre") or slug).strip(),
            "orden": int(orden) if isinstance(orden, (int, float)) else 100,
            "keywords": kws,
        })

    if not categorias:
        return {"ok": False, "modelo": MODELO_CATEGORIAS, "dominio": data.get("dominio"),
                "categorias": [], "error": "La IA no propuso categorias utilizables."}

    return {"ok": True, "error": None, "modelo": MODELO_CATEGORIAS,
            "dominio": data.get("dominio"), "categorias": categorias}


# ---------------------------------------------------------------------------
# Fase 2: investigar aranceles con web_search
# ---------------------------------------------------------------------------

def _investigar_aranceles_lote(client, categorias: list[dict], max_busquedas: int) -> tuple[dict, str | None]:
    """Una llamada web_search para un lote PEQUENO de categorias.

    Devuelve (resultados: {slug: {...}}, error: str|None). Aislar el lote evita
    que el JSON final se trunque por exceso de categorias en una sola respuesta.
    """
    lineas = []
    for c in categorias:
        kws = ", ".join((c.get("keywords") or [])[:8])
        ejemplo = f" (ejemplos: {kws})" if kws else ""
        lineas.append(f'- slug "{c["slug"]}": {c.get("nombre") or c["slug"]}{ejemplo}')
    listado = "\n".join(lineas)

    prompt = (
        "Eres un clasificador arancelario para importaciones a MEXICO. Para cada "
        "una de estas categorias de producto, investiga en fuentes oficiales "
        "mexicanas (preferentemente snice.gob.mx, sat.gob.mx, dof.gob.mx) la "
        "FRACCION ARANCELARIA de la TIGIE (formato NNNN.NN.NN, 8 digitos) y la "
        "TASA del IGI (Impuesto General de Importacion) en porcentaje aplicable a "
        "la importacion.\n\n"
        f"Categorias:\n{listado}\n\n"
        "REGLAS IMPORTANTES:\n"
        "- Usa los resultados reales de busqueda; NO inventes fracciones ni tasas.\n"
        "- Si no estas razonablemente seguro de la fraccion de una categoria, deja "
        "`fraccion` en null y explica en `nota` que quedo pendiente de determinar.\n"
        "- `confianza` es 0.0-1.0 segun que tan seguro estas de la fraccion.\n"
        "- `fuente_url` es la URL de donde obtuviste la fraccion (o null).\n\n"
        "Responde UNICAMENTE con un bloque JSON (sin texto antes ni despues) que "
        "sea un arreglo con esta forma exacta, UN objeto por categoria:\n"
        "```json\n"
        "[\n"
        "  {\n"
        '    "slug": "termos",\n'
        '    "fraccion": "9617.00.01",\n'
        '    "tasa_pct": 15,\n'
        '    "confianza": 0.8,\n'
        '    "fuente_url": "https://...",\n'
        '    "nota": "termos / vacuum flask"\n'
        "  }\n"
        "]\n"
        "```"
    )

    tool = {
        "type": WEBSEARCH_TOOL,
        "name": "web_search",
        "max_uses": max_busquedas,
    }
    if DOMINIOS_TIGIE:
        tool["allowed_domains"] = DOMINIOS_TIGIE

    try:
        response = client.messages.create(
            model=MODELO_ARANCELES,
            max_tokens=4096,
            thinking={"type": "disabled"},  # ver nota en proponer_categorias
            tools=[tool],
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.RateLimitError:
        return {}, ("Limite de tasa de la API alcanzado (web_search consume muchos "
                    "tokens). Espera ~1 minuto y reintenta.")
    except anthropic.APIError as e:
        return {}, f"Error de la API: {e}"

    data = _extraer_json(_texto_de_respuesta(response))
    if not isinstance(data, list):
        return {}, "La IA no devolvio un JSON valido de aranceles."

    resultados = {}
    for it in data:
        if not isinstance(it, dict):
            continue
        slug = _slugify(it.get("slug") or "")
        if not slug:
            continue
        fraccion = (it.get("fraccion") or "").strip() if it.get("fraccion") else None
        if fraccion and not FRACCION_RE.match(fraccion):
            fraccion = None  # formato invalido -> tratar como no encontrada
        resultados[slug] = {
            "fraccion": fraccion,
            "tasa_pct": _num(it.get("tasa_pct")),
            "confianza": _num(it.get("confianza")),
            "fuente_url": (it.get("fuente_url") or None),
            "nota": (it.get("nota") or "").strip() or None,
        }
    return resultados, None


def investigar_aranceles(categorias: list[dict]) -> dict:
    """Investiga fraccion TIGIE + IGI% por categoria via web_search, EN LOTES.

    Pedir 30+ categorias en una sola respuesta trunca el JSON final (excede
    max_tokens) -> antes fallaba con "no devolvio JSON valido". Aqui se parte en
    lotes de ARANCEL_BATCH y se unen los resultados; el fallo de un lote NO tumba
    a los demas (resultado parcial: las categorias sin fraccion quedan pendientes).

    `categorias`: [{slug, nombre, keywords?}, ...].
    Devuelve: {ok, error, modelo, resultados: {slug: {fraccion, tasa_pct,
    confianza, fuente_url, nota}}}. ok=False solo si NINGUN lote aporto nada.
    """
    if not categorias:
        return {"ok": True, "error": None, "modelo": MODELO_ARANCELES, "resultados": {}}
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {"ok": False, "error": "Falta ANTHROPIC_API_KEY en el entorno.",
                "modelo": MODELO_ARANCELES, "resultados": {}}

    client = anthropic.Anthropic()
    resultados: dict = {}
    errores: list[str] = []
    n_lotes = (len(categorias) + ARANCEL_BATCH - 1) // ARANCEL_BATCH
    for i in range(0, len(categorias), ARANCEL_BATCH):
        lote = categorias[i:i + ARANCEL_BATCH]
        # buscas suficientes para el lote, acotadas por MAX_BUSQUEDAS
        max_b = min(MAX_BUSQUEDAS, len(lote) + 3)
        res, err = _investigar_aranceles_lote(client, lote, max_b)
        resultados.update(res)
        if err:
            errores.append(f"lote {i // ARANCEL_BATCH + 1}/{n_lotes}: {err}")

    # ok=True si al menos un lote aporto resultados; error resume fallos parciales.
    error = "; ".join(dict.fromkeys(errores)) if errores else None
    ok = bool(resultados) or not errores
    return {"ok": ok, "error": error, "modelo": MODELO_ARANCELES, "resultados": resultados}


# ---------------------------------------------------------------------------
# Orquestador: propuesta completa (fase 1 + fase 2)
# ---------------------------------------------------------------------------

# Umbral de confianza minimo para proponer una fraccion como 'propuesto'.
CONFIANZA_MIN = float(os.getenv("CATALOGO_CONFIANZA_MIN", "0.5"))


def proponer_catalogo(descripciones: list[str], categorias_existentes: list[dict] | None = None) -> dict:
    """Corre fase 1 (categorias) y fase 2 (aranceles) y compone la propuesta.

    categorias_existentes (opcional): catalogo actual [{slug, keywords[]}] para que
    la fase 1 reutilice slugs en vez de duplicar (ver proponer_categorias).

    NO persiste nada. Devuelve:
      {ok, error, dominio, modelo, aviso_aranceles,
       categorias: [{slug, nombre, orden, keywords[], fraccion|None, tasa_pct|None,
                     confianza, fuente_url, nota, arancel_estado}]}

    `arancel_estado` en la propuesta es un hint para la UI:
      'propuesto' = fraccion valida con confianza suficiente (el usuario la
                    confirmara al aplicar) | 'pendiente' = sin fraccion (a determinar).
    """
    fase1 = proponer_categorias(descripciones, categorias_existentes=categorias_existentes)
    if not fase1["ok"]:
        return {"ok": False, "error": fase1["error"], "dominio": fase1.get("dominio"),
                "modelo": fase1["modelo"], "categorias": [], "aviso_aranceles": None}

    categorias = fase1["categorias"]
    fase2 = investigar_aranceles(categorias)
    resultados = fase2.get("resultados", {})
    if not fase2["ok"]:
        aviso = ("No se pudo completar la investigacion arancelaria "
                 f"({fase2['error']}). Las categorias quedan con arancel pendiente.")
    elif fase2.get("error"):
        aviso = ("Investigacion arancelaria parcial "
                 f"({fase2['error']}). Las categorias sin fraccion quedan pendientes.")
    else:
        aviso = None

    items = []
    for c in categorias:
        r = resultados.get(c["slug"], {})
        fraccion = r.get("fraccion")
        tasa = r.get("tasa_pct")
        confianza = r.get("confianza")
        # 'propuesto' solo si hay fraccion valida, tasa y confianza suficiente.
        if fraccion and tasa is not None and (confianza is None or confianza >= CONFIANZA_MIN):
            estado = "propuesto"
        else:
            estado = "pendiente"
            if not fraccion:
                tasa = None  # sin fraccion no proponemos tasa
        items.append({
            **c,
            "fraccion": fraccion,
            "tasa_pct": tasa,
            "confianza": confianza,
            "fuente_url": r.get("fuente_url"),
            "nota": r.get("nota"),
            "arancel_estado": estado,
        })

    return {"ok": True, "error": None, "dominio": fase1.get("dominio"),
            "modelo": fase1["modelo"], "aviso_aranceles": aviso, "categorias": items}


# ---------------------------------------------------------------------------
# Vision: describir productos desde su foto (para catalogos SKU + foto sin texto)
# ---------------------------------------------------------------------------

def _imagen_a_bloque(path: str) -> dict:
    """Lee una imagen y la devuelve como bloque `image` de Anthropic.

    Si Pillow esta disponible, la re-escala a VISION_MAX_SIDE y la recomprime a
    JPEG (baja mucho el costo/tokens). Si no, envia los bytes originales.
    """
    data = Path(path).read_bytes()
    media = "image/jpeg"
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((VISION_MAX_SIDE, VISION_MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
        data = buf.getvalue()
    except Exception:
        media = _MEDIA_POR_EXT.get(Path(path).suffix.lower(), "image/jpeg")
    b64 = base64.standard_b64encode(data).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}


def _describir_batch(batch: list[dict], client) -> dict:
    """Describe un lote de productos con vision.

    Devuelve {producto_id: {"descripcion": str, "tags": [str]}}.
    """
    content = []
    for i, it in enumerate(batch):
        etiqueta = f"Imagen {i}: SKU {it.get('sku') or '?'}"
        if it.get("medidas"):
            etiqueta += f", medidas {it['medidas']}"
        content.append({"type": "text", "text": etiqueta})
        try:
            content.append(_imagen_a_bloque(it["path"]))
        except Exception:
            content.append({"type": "text", "text": "(imagen no disponible)"})
    content.append({"type": "text", "text": (
        "Eres un catalogador de productos importados de China. Para CADA imagen:\n"
        "- `descripcion`: UNA frase corta (6-14 palabras, espanol) de QUE ES el "
        "producto, con tipo, material visible y rasgos clave utiles para "
        "clasificarlo. NO asumas un dominio fijo: puede ser CUALQUIER tipo de "
        "producto (flores, camaras de seguridad, cocina, banos, herramientas, "
        "mascotas, electronica, etc.). Identifica el tipo/subtipo MAS ESPECIFICO que "
        "se vea segun lo que sea: la especie de una flor (rosa, peonia...), el "
        "modelo/tipo de un aparato (camara domo IP, sarten antiadherente, mezcladora "
        "monomando de bano...), etc. Si no puedes determinar el subtipo con "
        "seguridad, usa el termino generico.\n"
        "- `tags`: 4-8 palabras clave en minusculas (espanol e ingles) para buscar "
        "el producto: incluye la especie/variedad, tipo, material, color y uso. Sin "
        "frases, solo terminos.\n"
        "No inventes marca ni texto que no se vea. Responde UNICAMENTE con un JSON "
        'array: [{"i": 0, "descripcion": "...", "tags": ["...", "..."]}].')})

    resp = client.messages.create(
        model=VISION_MODELO,
        max_tokens=1500,
        messages=[{"role": "user", "content": content}],
    )
    data = _extraer_json(_texto_de_respuesta(resp))
    out = {}
    if isinstance(data, list):
        for obj in data:
            if not isinstance(obj, dict):
                continue
            idx = obj.get("i")
            if isinstance(idx, bool) or not isinstance(idx, int):
                continue
            if not (0 <= idx < len(batch)):
                continue
            desc = (obj.get("descripcion") or "").strip()
            if not desc:
                continue
            tags, vistas = [], set()
            for t in obj.get("tags", []) or []:
                k = (str(t) or "").strip().lower()
                if k and k not in vistas:
                    vistas.add(k)
                    tags.append(k)
            out[batch[idx]["producto_id"]] = {"descripcion": desc, "tags": tags}
    return out


def describir_fotos(items: list[dict], progreso=None) -> dict:
    """Genera descripcion + tags desde las fotos de los productos (por lotes).

    `items`: [{producto_id, sku, medidas, path}, ...] (solo productos con foto).
    `progreso(hechos, total)`: callback opcional para reportar avance.
    Devuelve: {ok, error, aviso, modelo,
               resultados: {producto_id: {descripcion, tags:[...]}}}.
    """
    if not items:
        return {"ok": True, "error": None, "aviso": None,
                "modelo": VISION_MODELO, "resultados": {}}
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {"ok": False, "error": "Falta ANTHROPIC_API_KEY en el entorno.",
                "aviso": None, "modelo": VISION_MODELO, "resultados": {}}

    client = anthropic.Anthropic()
    resultados: dict = {}
    fallidos = 0
    total = len(items)
    for start in range(0, total, VISION_BATCH):
        batch = items[start:start + VISION_BATCH]
        try:
            resultados.update(_describir_batch(batch, client))
        except anthropic.RateLimitError:
            aviso = (f"Limite de tasa alcanzado tras {len(resultados)} de {total}. "
                     "Se guardan las hechas; reintenta para completar el resto.")
            return {"ok": True, "error": None, "aviso": aviso,
                    "modelo": VISION_MODELO, "resultados": resultados}
        except anthropic.APIError:
            fallidos += len(batch)
        if progreso:
            progreso(min(start + VISION_BATCH, total), total)

    aviso = f"{fallidos} imagen(es) fallaron y quedaron sin describir." if fallidos else None
    return {"ok": True, "error": None, "aviso": aviso,
            "modelo": VISION_MODELO, "resultados": resultados}
