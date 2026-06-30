"""
Modulo de extraccion semantica de productos usando Claude (Haiku 4.5).

Se usa como fallback cuando el parser heuristico de pdf_a_formato_hd.py
no logra extraer suficientes datos del JSON de Adobe (ej: PDFs con layout
no-tabular como catalogos visuales).

Estrategia:
  1. Recibe el structuredData.json de Adobe y la lista de figuras.
  2. Compacta el JSON: solo texto + coordenadas + paths (sin Font, etc.).
  3. Le pide a Claude Haiku que identifique cada producto: SKU, descripcion, FOB.
  4. Claude tambien asocia cada producto con la figura correspondiente
     usando las coordenadas Y (la figura mas cercana en la misma pagina).
  5. Devuelve [{sku, desc, fob, foto}, ...] listo para construir xlsx.

Costo aproximado por PDF: ~$0.005 USD con Haiku 4.5.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv


MODELO_DEFAULT = "claude-haiku-4-5"
# Max imagenes por request (Anthropic recomienda <=20 para mantener calidad)
LOTE_IMAGENES = 18


def codificar_imagen_b64(ruta: str) -> tuple[str, str]:
    """Devuelve (media_type, base64_data) para una imagen."""
    mt = mimetypes.guess_type(ruta)[0] or "image/png"
    with open(ruta, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    return mt, b64


def clasificar_figuras(
    figures_dir: str,
    modelo: str = MODELO_DEFAULT,
) -> dict[str, str]:
    """
    Clasifica cada figura del PDF como: producto, empaque, logo, diagrama, otro.

    Procesa en lotes de ~18 imagenes por request. Devuelve {nombre_archivo: tipo}.
    """
    if not os.path.isdir(figures_dir):
        return {}

    archivos = sorted(
        n for n in os.listdir(figures_dir)
        if n.lower().endswith((".png", ".jpg", ".jpeg"))
    )
    if not archivos:
        return {}

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")

    print(f"  Clasificando {len(archivos)} figuras en lotes de {LOTE_IMAGENES}...")
    client = anthropic.Anthropic()

    resultado: dict[str, str] = {}
    total_in = 0
    total_out = 0

    for inicio in range(0, len(archivos), LOTE_IMAGENES):
        lote = archivos[inicio:inicio + LOTE_IMAGENES]
        # Construir mensaje con imagenes + indices
        content: list[dict] = []
        for i, nombre in enumerate(lote):
            ruta = os.path.join(figures_dir, nombre)
            try:
                mt, b64 = codificar_imagen_b64(ruta)
            except Exception as e:
                print(f"    Error leyendo {nombre}: {e}")
                continue
            content.append({
                "type": "text",
                "text": f"\n[Imagen {i + 1}: {nombre}]",
            })
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mt, "data": b64},
            })

        content.append({
            "type": "text",
            "text": (
                "\n\nClasifica cada imagen anterior. Tipos posibles:\n"
                "- producto: muestra el articulo a vender. SIEMPRE vale 'producto' si:\n"
                "    * Hay al menos una foto/render del producto (aunque sea pequena)\n"
                "    * Es una vista renderizada en 3D del producto\n"
                "    * Hay cotas con flechas (647mm, 662mm) AL LADO del producto\n"
                "    * Hay varios productos del mismo tipo o colores juntos\n"
                "- empaque: SOLO la caja/bolsa/carton vacia, sin mostrar el producto\n"
                "- logo: logo de marca pequeno aislado\n"
                "- diagrama: SOLO valido si NO hay foto realista del producto;\n"
                "    es un dibujo tecnico en lineas (exploded view, plano industrial)\n"
                "- otro: certificaciones, sellos, texto suelto, iconos abstractos\n\n"
                "IMPORTANTE: en caso de duda entre 'producto' y 'diagrama',\n"
                "ELIGE 'producto'. Las cotas dimensionales son normales en fotos\n"
                "de catalogo y NO convierten una foto de producto en diagrama.\n\n"
                "Devuelve EXCLUSIVAMENTE un JSON valido (sin markdown):\n"
                '{"clasificaciones": [{"archivo": "fileoutpart0.png", "tipo": "producto"}]}'
            ),
        })

        resp = client.messages.create(
            model=modelo,
            max_tokens=4000,
            messages=[{"role": "user", "content": content}],
        )
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens

        texto = resp.content[0].text.strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", texto, re.DOTALL)
        if m:
            texto = m.group(1).strip()
        try:
            parsed = json.loads(texto)
            for item in parsed.get("clasificaciones", []):
                arch = item.get("archivo", "").strip()
                tipo = item.get("tipo", "otro").strip().lower()
                if arch:
                    resultado[arch] = tipo
        except json.JSONDecodeError as e:
            # Fallback: parsear linea por linea con regex
            print(f"    Aviso: JSON invalido en lote {inicio} ({e}), recuperando con regex")
            rescatadas = 0
            for m_item in re.finditer(
                r'"archivo"\s*:\s*"([^"]+)"\s*,\s*"tipo"\s*:\s*"([^"]+)"',
                texto,
            ):
                arch = m_item.group(1).strip()
                tipo = m_item.group(2).strip().lower()
                if arch:
                    resultado[arch] = tipo
                    rescatadas += 1
            print(f"    Rescatadas {rescatadas} clasificaciones via regex")

    if "haiku" in modelo.lower():
        # Costo Haiku 4.5: $1/MTok in (incluye imagenes), $5/MTok out
        costo = total_in / 1_000_000 * 1.0 + total_out / 1_000_000 * 5.0
        print(f"  Clasificacion: in={total_in} out={total_out} costo=${costo:.4f}")

    # Resumen
    from collections import Counter
    cuenta = Counter(resultado.values())
    print(f"  Tipos detectados: {dict(cuenta)}")
    return resultado


def compactar_structured_data(json_path: str) -> list[dict]:
    """
    Reduce el JSON de Adobe a una lista de elementos minimos:
    [{type: 'text'|'figure', page, y, text|file}, ...]

    Esto reduce ~10x el tamano del JSON que mandamos a Claude sin perder info
    relevante para la extraccion de productos.
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    elementos: list[dict] = []
    for e in data.get("elements", []):
        path = e.get("Path", "")
        bounds = e.get("Bounds")
        page = e.get("Page", 0)
        y = (bounds[1] + bounds[3]) / 2 if bounds else None

        if "/Figure" in path:
            files = e.get("filePaths") or []
            if files:
                elementos.append({
                    "tipo": "figura",
                    "p": page,
                    "y": round(y, 1) if y else None,
                    "archivo": files[0],
                })
        else:
            texto = (e.get("Text") or "").strip()
            texto = re.sub(r"_x000D_|\r|\n+", " ", texto)
            texto = " ".join(texto.split())
            if texto:
                elementos.append({
                    "tipo": "texto",
                    "p": page,
                    "y": round(y, 1) if y else None,
                    "t": texto,
                })
    return elementos


def construir_prompt(elementos: list[dict], figuras: list[str]) -> str:
    """Construye el prompt para Claude con los elementos extraidos y las figuras disponibles."""
    return f"""Eres un asistente que extrae productos de cotizaciones de proveedores.

Te paso el contenido de un PDF de cotizacion que Adobe extrajo. Cada elemento
tiene tipo ('texto' o 'figura'), pagina (p), coordenada Y (y, donde mayor=mas arriba),
y el texto o archivo de imagen.

Tu tarea: identificar (a) el PROVEEDOR (seller) que emite la cotizacion y
(b) CADA PRODUCTO distinto del catalogo/cotizacion. Devolver todo como JSON.

REGLAS:

REGLA CRITICA - FORMATO "PI CHINO" (frecuente en Hangzhou/Foshan/Shenzhen/Ningbo).
Mira los HEADERS de la tabla principal. Si ves columnas tipo:
  "Cantidad" (o "Cant idad/piezas")
  "Volumen Total (m³)" (a veces combinada con peso: "Volumen Total / Peso bruto")
entonces ESOS VALORES SON TOTALES DEL PEDIDO COMPLETO, no per-carton.

Ejemplo anti-pattern (ESTO ES INCORRECTO, NO LO HAGAS):
  PDF dice: Cantidad=500, Volumen Total="69 CBM / 5,750 kg", Peso bruto=11.5 kg
  Mapeo INCORRECTO: moq="500 pcs", cbm=69, nw_caja_kg=5750, peso_kg=11.5
  Por que es incorrecto: 5750 kg NO cabe en una caja; 69 m³ es un contenedor entero.

Mapeo CORRECTO (caso A — producto grande, 1 pza por caja):
  pzas_caja = 1 (producto >40 cm Y packing "Caja de carton kraft" sin numero)
  peso_kg   = 11.5 (la columna "Peso bruto" separada suele ser per-unit)
  cbm       = Volumen_Total / num_cartones = 69 / (500/1) = 0.138
  pzas_40hq = 500  (porque Volumen Total entre 55-75 m³ = pedido llena 1x40HQ)
  pzas_20ft = Cantidad (si Volumen Total entre 25-35 m³)
  moq, nw_caja_kg, gw_caja_kg: OMITELOS. No hay columnas per-carton explicitas.

Caso B — producto chico, varias piezas por caja (ej. juguetes):
  PDF dice: Cantidad=1000, Volumen Total="5 CBM/600 kg", Peso bruto=0.43 kg,
            Carton dims="31x31x4.6 cm" (=> 0.044 m³ por caja aprox)
  num_cartones = Cantidad / pzas_caja  (= 1000 / 20 = 50 cartones)
  cbm = Volumen_Total / num_cartones (= 5 / 50 = 0.1)  [NO uses Vol/Cantidad
        cuando pzas_caja > 1; eso te da CBM por pieza, no por caja]
  pzas_40hq = OMITIR cuando Volumen Total < 50 m³ (el pedido no llena un
              contenedor; el sistema derivara pzas_40hq despues con cbm+
              pzas_caja). NO uses "Cantidad" como pzas_40hq en este caso.
  pzas_caja: del campo "Packing" si dice tipo "20 pzas/caja" o de Carton
             dims si calza; sino, dejar vacio.
  peso_kg = 0.43 (per-unit)
  moq, nw_caja_kg, gw_caja_kg: OMITELOS si no hay columna explicita.

MOQ: solo lo poblas si hay un header EXPLICITO "MOQ", "Min Order" o
"Minimum Order Quantity". La columna "Cantidad" / "Cant idad" en estos PDFs
es el tamano del pedido, NO el MOQ. Si no hay header MOQ, deja moq="".

NW/GW caja: solo lo poblas si hay un header EXPLICITO "N.W.", "G.W.",
"Net Weight", "Gross Weight" referente al CARTON master. NO uses el segundo
numero de "Volumen Total" (eso es kg totales del pedido). NO uses peso_kg
como gw_caja_kg.

Si NO ves columnas "Cantidad" + "Volumen Total" como columnas separadas, este
caso no aplica: usa las reglas normales abajo.

1. PROVEEDOR (seller): es la empresa que VENDE/emite la cotizacion. Suele
   aparecer como "Seller", "Vendor", "From", o como company name en el
   encabezado. NO confundas con "Buyer" (comprador, ej: Fortuna Abadi).
   Si aparece dos veces (encabezado + tabla), usa la version mas completa
   con sufijo legal (CO.,LTD, S.A., Inc., GmbH, etc).
2. Cada producto tipicamente tiene: nombre, codigo SKU, precio FOB en USD,
   medidas/tamano, material, peso, MOQ, color, packing, una imagen.
3. Ignora encabezados de pagina, datos administrativos del proveedor
   (telefono, email, banco), terminos generales (validity), logos, footers.
4. Si un producto tiene variantes (tamanos, colores) con precios distintos,
   listalas como productos separados.
5. Si no hay codigo SKU explicito, deja "sku" como cadena vacia.
6. Para "foto": elige el archivo de figura mas cercano al producto por
   coordenada Y en la misma pagina. Si no hay figura cercana, "".
7. Para "fob": numero sin "$" ni "US$". Si no lo encuentras, null.
8. Para "pzas_caja": cuantas piezas hay por carton master. Buscar columnas tipo
   "QTY/CARTON", "PCS/CTN", "pcs per carton" o frases tipo "12 pcs/box". Si
   packing dice "1pc/..." entonces pzas_caja = 1. Si no aparece, dejar "".
9. Para "nw_caja_kg" y "gw_caja_kg": peso neto y bruto del carton master en KG.
   Aparecen como "G.W./N.W.", "Gross Weight / Net Weight" o similar (frecuente
   en cotizaciones Alibaba como "20/18 KGS"). Convencion: el PRIMER numero es
   G.W. (bruto) y el SEGUNDO N.W. (neto). Si solo hay un valor sin etiquetar,
   asumir G.W. y dejar nw_caja_kg vacio. Si no aparece, dejar "".
10. Para campos opcionales, deja "" si no aparecen en el PDF.

Devuelve EXCLUSIVAMENTE un JSON valido (sin texto adicional ni markdown):
{{
  "seller": "Xi'an Canal Fashion BIO-TECH CO.,LTD",
  "buyer": "Fortuna Abadi",
  "productos": [
    {{
      "sku": "XDB-490M1",
      "desc": "Plastic pet kennel Medium, no door",
      "fob": 8.00,
      "foto": "figures/fileoutpart5.png",
      "medidas": "L750xW640xH516 mm",
      "material": "PP",
      "peso_kg": "5",
      "color": "Blue roof + white wall",
      "moq": "300 pcs",
      "packing": "1pc / brown carton box",
      "carton": "655x210x520mm",
      "cbm": "0.07",
      "pzas_caja": "1",
      "nw_caja_kg": "5",
      "gw_caja_kg": "5.5",
      "pzas_20ft": "380",
      "pzas_40hq": "950",
      "lead_time": "25 days"
    }},
    ...
  ]
}}

Si no podes identificar al seller con certeza, deja "seller": "".

Figuras disponibles en el PDF:
{json.dumps(figuras, indent=2)}

Elementos del PDF (texto y figuras con coordenadas):
{json.dumps(elementos, ensure_ascii=False, separators=(',', ':'))}
"""


def extraer_con_claude(
    json_path: str,
    figures_dir: str,
    modelo: str = MODELO_DEFAULT,
    max_tokens_salida: int = 32000,
    clasificar: bool = True,
) -> dict:
    """
    Llama a Claude para extraer productos de un PDF cuyo JSON ya extrajo Adobe.

    Si clasificar=True, primero clasifica todas las figuras visualmente y solo
    pasa las de tipo 'producto' al extractor (filtra logos, empaques, diagramas).

    Devuelve {"seller": str, "buyer": str, "productos": [{sku, desc, fob, foto, ...}]}.
    """
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")

    # Paso 1: clasificar figuras visualmente (filtra logos, empaques, diagramas)
    clasificaciones: dict[str, str] = {}
    if clasificar and os.path.isdir(figures_dir):
        # Cache: si ya existe clasificacion previa, reusar
        cache_path = os.path.join(os.path.dirname(figures_dir), "_clasificacion_figuras.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as f:
                    clasificaciones = json.load(f)
                from collections import Counter
                cuenta = Counter(clasificaciones.values())
                print(f"  Reuso clasificacion cacheada: {dict(cuenta)}")
            except Exception:
                clasificaciones = {}

        if not clasificaciones:
            clasificaciones = clasificar_figuras(figures_dir, modelo=modelo)
            Path(cache_path).write_text(
                json.dumps(clasificaciones, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    elementos = compactar_structured_data(json_path)

    # Filtrar elementos: solo dejar figuras tipo 'producto'
    if clasificaciones:
        filtrados: list[dict] = []
        for e in elementos:
            if e["tipo"] == "figura":
                archivo = e["archivo"]
                # archivo viene como "figures/fileoutpart12.png"
                solo_nombre = archivo.split("/")[-1]
                tipo = clasificaciones.get(solo_nombre, "otro")
                if tipo != "producto":
                    continue
                # Marcar para el modelo
                e["tipo_visual"] = tipo
            filtrados.append(e)
        elementos = filtrados

    figuras = sorted([
        f"figures/{n}"
        for n, tipo in clasificaciones.items()
        if tipo == "producto"
    ]) if clasificaciones else sorted([
        f"figures/{n}" for n in os.listdir(figures_dir)
        if n.lower().endswith((".png", ".jpg", ".jpeg"))
    ]) if os.path.isdir(figures_dir) else []

    prompt = construir_prompt(elementos, figuras)
    print(f"  Prompt: {len(prompt)} chars, {len(elementos)} elementos, {len(figuras)} figuras producto")

    client = anthropic.Anthropic()
    # Streaming requerido cuando max_tokens es alto (>~8k); evita el timeout de 10 min.
    with client.messages.stream(
        model=modelo,
        max_tokens=max_tokens_salida,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        resp = stream.get_final_message()

    texto = resp.content[0].text.strip()
    print(f"  Tokens: in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
    # Costo aproximado Haiku 4.5: $1/MTok in, $5/MTok out
    if "haiku" in modelo.lower():
        costo = (resp.usage.input_tokens / 1_000_000) * 1.0 + (resp.usage.output_tokens / 1_000_000) * 5.0
        print(f"  Costo estimado: ${costo:.4f} USD")

    # Claude a veces envuelve en ```json ... ```
    m = re.search(r"```(?:json)?\s*(.*?)```", texto, re.DOTALL)
    if m:
        texto = m.group(1).strip()

    try:
        parsed = json.loads(texto)
    except json.JSONDecodeError as e:
        # Guardar respuesta para debug
        debug_path = os.path.join(figures_dir, "..", "_claude_response_debug.txt")
        Path(debug_path).write_text(texto, encoding="utf-8")
        raise RuntimeError(
            f"Claude devolvio JSON invalido. Guardado en {debug_path}. Error: {e}"
        )

    productos = parsed.get("productos", [])
    seller = (parsed.get("seller") or "").strip()
    buyer = (parsed.get("buyer") or "").strip()
    print(f"  Productos extraidos por Claude: {len(productos)}")
    if seller:
        print(f"  Seller detectado: {seller}")
    if buyer:
        print(f"  Buyer detectado: {buyer}")

    # Propagar foto a variantes que comparten prefijo SKU.
    # Ej: XDB-490S1, XDB-490M1, XDB-490S2 todos comparten "XDB-490" -> mismo grupo.
    # Si alguna variante del grupo tiene foto, las que no la tienen la heredan.
    def prefijo_sku(sku: str) -> str:
        """
        Devuelve el prefijo numerico raiz para agrupar variantes.
        Ej: XDB-432, XDB-432S, XDB-432XS, XDB-432-V2 -> "XDB-432"
            XDB-490S1, XDB-490M1 -> "XDB-490"
        """
        if not sku:
            return ""
        s = sku.strip().upper()
        # Tomar letras+dash+digitos iniciales, ignorar sufijos como S/M/L/XS/XL/numeros tras letras
        m = re.match(r"^([A-Z]+[-]?\d+)", s)
        if not m:
            return s
        prefijo = m.group(1)
        # Si despues hay letras (S, M, L, S1, M1, etc), las quitamos
        # Si despues hay -digitos (sub-variante), tambien las quitamos
        return prefijo

    # Paso 1: propagar dentro de cada grupo (basado en fotos asignadas por Claude)
    def propagar_dentro_de_grupos() -> int:
        fotos_por_grupo: dict[str, str] = {}
        for p in productos:
            pref = prefijo_sku(p.get("sku") or "")
            foto = (p.get("foto") or "").strip()
            if pref and foto and pref not in fotos_por_grupo:
                fotos_por_grupo[pref] = foto
        n = 0
        for p in productos:
            if not (p.get("foto") or "").strip():
                pref = prefijo_sku(p.get("sku") or "")
                if pref in fotos_por_grupo:
                    p["foto"] = fotos_por_grupo[pref]
                    p["foto_heredada"] = True
                    n += 1
        return n

    propagadas = propagar_dentro_de_grupos()

    # Paso 2: fallback - asignar a un producto sin foto la primera figura
    # tipo "producto" que nadie haya usado. Despues de cada asignacion,
    # re-propagar dentro del grupo para que las variantes del mismo SKU
    # tambien hereden.
    usadas = {(p.get("foto") or "").strip() for p in productos if p.get("foto")}
    no_usadas = [
        f"figures/{n}"
        for n, tipo in clasificaciones.items()
        if tipo == "producto" and f"figures/{n}" not in usadas
    ] if clasificaciones else []

    asignadas_fallback = 0
    for p in productos:
        if not (p.get("foto") or "").strip() and no_usadas:
            p["foto"] = no_usadas.pop(0)
            p["foto_fallback"] = True
            asignadas_fallback += 1

    # Paso 3: volver a propagar (ahora con las fotos de fallback como semilla)
    propagadas += propagar_dentro_de_grupos()

    if propagadas or asignadas_fallback:
        print(f"  Fotos propagadas por prefijo SKU: {propagadas}")
        print(f"  Fotos asignadas por fallback: {asignadas_fallback}")

    return {"seller": seller, "buyer": buyer, "productos": productos}


# ============================================================
# Fallback de Vision: renderiza paginas del PDF y se las pasa a Claude
# como imagenes. Ultima opcion cuando Adobe + Claude/JSON dan 0 productos.
# ============================================================


PROMPT_VISION = """Eres un asistente que extrae cotizaciones de productos a partir de imagenes de paginas de PDF.

Te paso N imagenes correspondientes a las paginas (ordenadas) de una cotizacion. Tu tarea:

1. Identifica el SELLER (vendedor/proveedor) si aparece (header, firma, "From:").
2. Identifica el BUYER (comprador/destinatario) si aparece ("To:", "Attn:").
3. Por cada PRODUCTO listado en la cotizacion, extrae estos campos:
   - sku: codigo de modelo/item (string, sin espacios extra). Ej: "XDB-490", "GH-2P068".
   - desc: descripcion corta y clara del producto (string).
   - fob: precio FOB unitario en USD (numero. ej 1.23). Si hay varios precios por cantidad,
     toma el MAS BAJO listado (corresponde a la cantidad mas alta). Sin simbolos.
   - medidas: dimensiones del producto. Ej: "30x20x10cm" (string).
   - material: material principal. Ej: "PP plastic", "ABS+PC".
   - peso_kg: peso por unidad en kg (numero o string si vino con unidades raras).
   - color: color o variantes disponibles.
   - moq: cantidad minima de pedido (string, ej "500 pcs").
   - packing: como se empaca cada unidad. Ej: "1pc/PE bag/colorbox".
   - carton: dimensiones del carton master. Ej: "55x40x30cm".
   - cbm: metros cubicos por carton master (numero).
   - pzas_caja: piezas por carton master (numero). BUSCAR especificamente columnas
     tipo "QTY/CARTON", "PCS/CTN", "pcs per carton", "qty/box", "pcs/carton" o frases
     como "12 pcs/box". Si el packing dice "1pc/..." => pzas_caja = 1. Si no aparece, omitir.
   - nw_caja_kg: peso neto del carton master en KG (numero). Aparece como "N.W.",
     "Net Weight" o el SEGUNDO valor en "G.W./N.W.: 20/18 KGS". Si no aparece, omitir.
   - gw_caja_kg: peso bruto del carton master en KG (numero). Aparece como "G.W.",
     "Gross Weight" o el PRIMER valor en "G.W./N.W.: 20/18 KGS". Si no aparece, omitir.
   - pzas_20ft: piezas por contenedor 20ft (numero).
   - pzas_40hq: piezas por contenedor 40HQ (numero).
   - lead_time: tiempo de entrega. Ej: "30 days".

4. Si un campo no aparece, omitelo (no inventes). Numeros como numeros JSON, no strings.

5. Si la cotizacion lista variantes del mismo modelo (ej. mismo SKU con tallas
   S/M/L o colores), genera UNA FILA POR VARIANTE con SKU diferenciado.

6. FORMATO "PI CHINO" (Hangzhou/Foshan/Shenzhen/Ningbo). Si la tabla tiene
   columnas "Cantidad" + "Volumen Total (m³)" + "Peso bruto" como columnas
   SEPARADAS, esos valores son TOTALES DEL PEDIDO, NO per-carton. Ejemplo:
     PDF: Cantidad=500, Volumen Total="69 CBM/5,750 kg", Peso bruto=11.5 kg
     MAL: moq="500 pcs", cbm=69, nw_caja_kg=5750
     BIEN: pzas_40hq=500 (cuando V.T. entre 55-75 m³), peso_kg=11.5,
           cbm=0.138 (=69/500 si pzas_caja=1), pzas_caja=1 (si producto
           mide >40 cm y packing dice "Caja de carton kraft"), nw_caja_kg
           y gw_caja_kg OMITIDOS (no inventes dividiendo totales).
   Si Volumen Total esta entre 25-35 m³ usar pzas_20ft en vez de pzas_40hq.

DEVUELVE UN SOLO JSON VALIDO con esta forma exacta (sin texto antes ni despues, sin ```):
{
  "seller": "...",
  "buyer": "...",
  "productos": [
    {"sku": "...", "desc": "...", "fob": 1.23, ...},
    ...
  ]
}
"""


def renderizar_paginas_pdf(
    pdf_path: str,
    max_pages: int = 10,
    dpi: int = 150,
) -> tuple[list[bytes], int]:
    """Renderiza las primeras `max_pages` paginas del PDF como PNG.

    Devuelve (lista_de_bytes_png, total_paginas_del_pdf).
    """
    import io
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_path)
    n_total = len(pdf)
    n = min(n_total, max_pages)
    scale = dpi / 72.0
    paginas: list[bytes] = []
    for i in range(n):
        page = pdf[i]
        bitmap = page.render(scale=scale)
        pil = bitmap.to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG", optimize=True)
        paginas.append(buf.getvalue())
    pdf.close()
    return paginas, n_total


def extraer_con_vision(
    pdf_path: str,
    modelo: str = MODELO_DEFAULT,
    max_pages: int = 10,
    dpi: int = 150,
    max_tokens_salida: int = 16000,
) -> dict:
    """Renderiza las paginas del PDF a PNG y pide a Claude extraer productos.

    Ultima opcion cuando Adobe Extract no detecta tablas y el flow JSON-based
    no encuentra productos. Mismo shape de salida que extraer_con_claude.

    Limitacion: no asigna fotos a los productos (no recortamos regiones de las
    paginas). El usuario puede subir foto por producto despues con el endpoint
    POST /api/productos/{id}/foto.
    """
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")

    paginas_bytes, n_total = renderizar_paginas_pdf(pdf_path, max_pages=max_pages, dpi=dpi)
    if not paginas_bytes:
        return {"seller": "", "buyer": "", "productos": []}
    print(f"  Paginas renderizadas: {len(paginas_bytes)}/{n_total} (dpi={dpi})")

    content: list[dict] = [{"type": "text", "text": PROMPT_VISION}]
    for png_bytes in paginas_bytes:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(png_bytes).decode("utf-8"),
            },
        })

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=modelo,
        max_tokens=max_tokens_salida,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        resp = stream.get_final_message()

    print(f"  Tokens Vision: in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
    if "haiku" in modelo.lower():
        costo = (resp.usage.input_tokens / 1_000_000) * 1.0 + (resp.usage.output_tokens / 1_000_000) * 5.0
        print(f"  Costo estimado Vision: ${costo:.4f} USD")

    texto = resp.content[0].text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", texto, re.DOTALL)
    if m:
        texto = m.group(1).strip()

    try:
        parsed = json.loads(texto)
    except json.JSONDecodeError as e:
        debug_path = os.path.join(os.path.dirname(pdf_path), "_vision_response_debug.txt")
        Path(debug_path).write_text(texto, encoding="utf-8")
        raise RuntimeError(
            f"Claude Vision devolvio JSON invalido. Guardado en {debug_path}. Error: {e}"
        )

    productos = parsed.get("productos", []) or []
    seller = (parsed.get("seller") or "").strip()
    buyer = (parsed.get("buyer") or "").strip()
    print(f"  Productos extraidos por Vision: {len(productos)}")
    if seller:
        print(f"  Seller detectado (Vision): {seller}")
    if buyer:
        print(f"  Buyer detectado (Vision): {buyer}")

    return {"seller": seller, "buyer": buyer, "productos": productos}


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Uso: python extraer_con_claude.py <carpeta_adobe_extract>")
    carpeta = os.path.abspath(sys.argv[1])
    json_path = os.path.join(carpeta, "structuredData.json")
    figures_dir = os.path.join(carpeta, "figures")
    if not os.path.exists(json_path):
        sys.exit(f"No existe: {json_path}")

    resultado = extraer_con_claude(json_path, figures_dir)
    productos = resultado["productos"]
    print()
    print(f"Extraidos {len(productos)} productos:")
    for i, p in enumerate(productos[:20], 1):
        sku = p.get("sku") or ""
        desc = (p.get("desc") or "")[:60]
        fob = p.get("fob")
        foto = p.get("foto") or ""
        print(f"  {i:3}. sku={sku:15} fob={fob!s:>7}  foto={foto:30} desc={desc!r}")
    if len(productos) > 20:
        print(f"  ... ({len(productos) - 20} mas)")

    salida = os.path.join(carpeta, "productos_claude.json")
    Path(salida).write_text(json.dumps(productos, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nGuardado en: {salida}")


if __name__ == "__main__":
    main()
