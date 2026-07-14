"""Recupera fotos de producto embebidas en celdas de PDFs tipo-tabla.

Contexto del problema
---------------------
Algunas cotizaciones vienen como UNA tabla que ocupa varias paginas, con la foto
de cada producto DENTRO de su celda (columna "Picture"). Cuando ese PDF es
escaneado/aplanado, Adobe Extract clasifica las imagenes como contenido de tabla
y rasteriza la tabla entera por pagina en `tables/*.png`, dejando `figures/` casi
vacio. Resultado: ni el parser heuristico ni Claude tienen figuras que asociar y
los productos entran SIN foto (aunque el texto/OCR salga perfecto).

Solucion
--------
Cuando `figures/` viene escaso, sacamos las imagenes raster embebidas del PDF con
PyMuPDF y las inyectamos en `structuredData.json` como elementos `Figure` (con
`Bounds` en coordenadas Adobe). Asi el matcheo por posicion que YA existe
(`pdf_a_formato_hd.parsear_filas` y el flujo `extraer_con_claude`) les asigna su
producto por cercania en Y -- exactamente como ya funciona con los PDFs cuyas
fotos si vienen como figuras sueltas.

Notas de implementacion
-----------------------
- Se renderiza la REGION (bbox) de cada imagen, no `extract_image(xref)`: en
  tablas PyMuPDF suele reportar un xref equivocado (varias celdas comparten uno
  falso), pero la posicion siempre es correcta.
- Coordenadas: PyMuPDF usa origen arriba-izquierda; Adobe usa abajo-izquierda.
  Se voltea: `y_adobe = alto_pagina - y_pymupdf`.
- Idempotente: si ya se inyectaron figuras `_embebida`, no repite.
- Solo se dispara como fallback (ver `figuras_escasas`), nunca sobre PDFs cuyo
  `figures/` ya trae las fotos.
"""
from __future__ import annotations

import json
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None

_EXT_IMG = (".png", ".jpg", ".jpeg", ".gif", ".bmp")


def figuras_escasas(carpeta_extract, minimo: int = 3) -> bool:
    """True si Adobe no emitio (casi) figuras propias -> conviene el fallback.

    Ignora las figuras `embed_*` que este mismo modulo pudo haber creado antes.
    """
    figdir = Path(carpeta_extract) / "figures"
    if not figdir.is_dir():
        return True
    propias = sum(
        1 for f in figdir.iterdir()
        if f.suffix.lower() in _EXT_IMG and not f.name.startswith("embed_")
    )
    return propias < minimo


def _candidatas(pg, area_pagina: float) -> list[dict]:
    """Imagenes raster de una pagina que parecen foto de producto: ni minusculas
    (iconos, brackets de cota), ni casi-pagina-completa (fondos), ni barras muy
    alargadas."""
    out = []
    for im in pg.get_image_info(xrefs=True):
        b = im["bbox"]
        w = b[2] - b[0]
        h = b[3] - b[1]
        if w < 28 or h < 28:
            continue
        if w * h > 0.30 * area_pagina:
            continue
        rel = h / w if w else 0
        if rel < 0.3 or rel > 3.5:
            continue
        out.append({"bbox": (b[0], b[1], b[2], b[3]), "w": w, "h": h})
    return out


def recuperar_figuras_embebidas(
    pdf_path,
    carpeta_extract,
    *,
    escala: float = 4.0,
    verbose: bool = True,
) -> int:
    """Extrae las fotos embebidas del PDF y las inyecta en structuredData.json
    como figuras. Devuelve cuantas inyecto (0 si no hizo nada).

    No falla el pipeline si algo sale mal: captura y devuelve 0.
    """
    if fitz is None:
        if verbose:
            print("  (PyMuPDF no disponible; no se recuperan fotos embebidas)")
        return 0

    carpeta_extract = Path(carpeta_extract)
    json_path = carpeta_extract / "structuredData.json"
    if not json_path.exists():
        return 0

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return 0

    elementos = data.get("elements", [])
    if any(e.get("_embebida") for e in elementos):
        if verbose:
            print("  Figuras embebidas ya inyectadas antes; se omite.")
        return 0

    alturas = {p.get("page_number"): p.get("height") for p in (data.get("pages") or [])}
    figdir = carpeta_extract / "figures"
    figdir.mkdir(parents=True, exist_ok=True)

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        if verbose:
            print(f"  No pude abrir el PDF con PyMuPDF: {e}")
        return 0

    try:
        # NO filtramos watermark/logo por tamaño aqui: en catalogos las fotos de
        # producto son uniformes y un filtro por tamaño repetido termina tirando
        # fotos reales. El descarte de logos/watermarks/empaques lo hace aguas
        # abajo la clasificacion visual de figuras (extraer_con_claude, que el
        # pipeline fuerza con --claude). Aqui solo aplicamos el filtro geometrico
        # de _candidatas y un tope de seguridad.
        MAX_FIG = 800
        nuevos = []
        idx = 0
        for pi in range(doc.page_count):
            pg = doc[pi]
            H = alturas.get(pi) or pg.rect.height
            for c in _candidatas(pg, pg.rect.width * pg.rect.height):
                if idx >= MAX_FIG:
                    break
                x0, y0, x1, y1 = c["bbox"]
                try:
                    pix = pg.get_pixmap(
                        matrix=fitz.Matrix(escala, escala),
                        clip=fitz.Rect(x0, y0, x1, y1),
                    )
                    (figdir / f"embed_{idx}.png").write_bytes(pix.tobytes("png"))
                except Exception:
                    continue
                nuevos.append({
                    "Path": "//Document/Figure",
                    "Page": pi,
                    "Bounds": [x0, H - y1, x1, H - y0],  # voltea a coords Adobe
                    "filePaths": [f"figures/embed_{idx}.png"],
                    "_embebida": True,
                })
                idx += 1
    finally:
        doc.close()

    if nuevos:
        elementos.extend(nuevos)
        data["elements"] = elementos
        json_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if verbose:
        print(f"  Fotos embebidas recuperadas e inyectadas: {len(nuevos)}")
    return len(nuevos)
