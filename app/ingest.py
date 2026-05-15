"""
Ingest de productos desde xlsx intermedios (los que produce pdf_a_formato_hd.py
en el paso 3) a la BD SQLite.

Estrategia:
- Idempotente: si (proveedor, sku) ya existe, actualiza datos en vez de duplicar.
- Copia las imagenes desde donde esten al directorio data/fotos/.
- No toca el campo marcado_cotizar al actualizar (preserva eleccion del usuario).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import openpyxl
from sqlalchemy.orm import Session

from app.modelos import Proveedor, Producto, Foto


def _cargar_meta(xlsx_path: str) -> dict:
    """Lee el .meta.json junto al xlsx intermedio. Devuelve {} si no existe o falla."""
    meta_path = Path(xlsx_path + ".meta.json")
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def resolver_nombre_proveedor(xlsx_path: str, fallback: str | None = None) -> str:
    """
    Resuelve el nombre del proveedor en este orden:
      1. .meta.json junto al xlsx (key 'seller', extraido del PDF con Claude).
      2. fallback si se paso.
      3. Nombre del archivo limpio (legacy).
    """
    seller = (_cargar_meta(xlsx_path).get("seller") or "").strip()
    if seller:
        return seller[:200]
    if fallback:
        return fallback
    return Path(xlsx_path).stem.replace("_intermedio_", "").replace("_", " ")[:60]


def resolver_archivo_pdf(xlsx_path: str) -> str:
    """
    Resuelve el nombre del PDF original del que vino este intermedio.
      1. .meta.json key 'pdf_original'.
      2. Fallback: el nombre del .xlsx intermedio (legacy, lo que se guardaba antes).
    """
    pdf_original = (_cargar_meta(xlsx_path).get("pdf_original") or "").strip()
    if pdf_original:
        return pdf_original
    return Path(xlsx_path).name


# Columnas esperadas en el xlsx intermedio
COL_FOTO = 1
COL_SKU = 2
COL_DESC = 3
COL_MEDIDAS = 4
COL_MATERIAL = 5
COL_PESO = 6
COL_COLOR = 7
COL_MOQ = 8
COL_PACKING = 9
COL_CARTON = 10
COL_CBM = 11
COL_PZAS20 = 12
COL_PZAS40 = 13
COL_LEAD = 14
COL_FOB = 15


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def _to_int(v) -> int | None:
    f = _to_float(v)
    return int(f) if f is not None else None


def _extraer_imagenes_xlsx(xlsx_path: str) -> dict[int, bytes]:
    """
    Devuelve {fila_excel: bytes_de_imagen} para imagenes ancladas en col A.
    Lee el ZIP del xlsx directamente para obtener bytes.
    """
    resultado: dict[int, bytes] = {}
    with zipfile.ZipFile(xlsx_path) as z:
        nombres = set(z.namelist())
        if "xl/drawings/drawing1.xml" not in nombres:
            return {}
        if "xl/drawings/_rels/drawing1.xml.rels" not in nombres:
            return {}

        rels_xml = z.read("xl/drawings/_rels/drawing1.xml.rels").decode("utf-8")
        ns_rels = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
        rid_a_target = {}
        for rel in ET.fromstring(rels_xml).findall("r:Relationship", ns_rels):
            rid_a_target[rel.get("Id")] = rel.get("Target")

        drawing_xml = z.read("xl/drawings/drawing1.xml").decode("utf-8")
        ns = {
            "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }
        root = ET.fromstring(drawing_xml)
        anchors = []
        for tag in ("oneCellAnchor", "twoCellAnchor"):
            anchors.extend(root.findall(f"xdr:{tag}", ns))

        for a in anchors:
            from_node = a.find("xdr:from", ns)
            pic = a.find("xdr:pic", ns)
            if from_node is None or pic is None:
                continue
            col = int(from_node.find("xdr:col", ns).text)
            if col != 0:
                continue
            fila = int(from_node.find("xdr:row", ns).text) + 1
            blip = pic.find("xdr:blipFill/a:blip", ns)
            rid = blip.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
            )
            target = rid_a_target.get(rid, "")
            if target.startswith("/"):
                ruta_zip = target.lstrip("/")
            else:
                import os
                ruta_zip = os.path.normpath(
                    os.path.join("xl/drawings", target)
                ).replace("\\", "/")
            if ruta_zip in nombres:
                resultado[fila] = z.read(ruta_zip)
    return resultado


def ingestar_xlsx_intermedio(
    session: Session,
    xlsx_path: str,
    nombre_proveedor: str | None,
    fotos_destino: str,
) -> int:
    """
    Lee un xlsx intermedio (el output de pdf_a_formato_hd.py paso 3) y lo
    inserta/actualiza en la BD.

    Si nombre_proveedor es None, se resuelve via resolver_nombre_proveedor
    (lee .meta.json o cae al nombre del archivo).

    Devuelve el numero de productos NUEVOS insertados (no incluye actualizados).
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active

    nombre_proveedor = resolver_nombre_proveedor(xlsx_path, fallback=nombre_proveedor)
    archivo_pdf = resolver_archivo_pdf(xlsx_path)

    prov = session.query(Proveedor).filter_by(nombre=nombre_proveedor).first()
    if prov is None:
        prov = Proveedor(
            nombre=nombre_proveedor,
            archivo_pdf=archivo_pdf,
        )
        session.add(prov)
        session.flush()
    elif prov.archivo_pdf != archivo_pdf:
        # Mantener actualizado el path al PDF original (puede cambiar entre re-ingestas)
        prov.archivo_pdf = archivo_pdf

    imagenes = _extraer_imagenes_xlsx(xlsx_path)

    fotos_dir = Path(fotos_destino)
    fotos_dir.mkdir(parents=True, exist_ok=True)

    import hashlib

    nuevos = 0
    for fila in range(2, ws.max_row + 1):
        sku = ws.cell(fila, COL_SKU).value or ""
        sku = str(sku).strip()
        desc = ws.cell(fila, COL_DESC).value or ""
        desc = str(desc).strip()
        if not (sku or desc):
            continue

        # Si no hay SKU real, generamos uno sintetico determinista a partir
        # de la descripcion. Mantiene idempotencia (re-ingestar produce el
        # mismo SKU) y evita la colision de UNIQUE(proveedor_id, sku='')
        # cuando hay varios productos sin SKU del mismo proveedor.
        sku_sintetico = False
        if not sku:
            h = hashlib.md5(desc.encode("utf-8")).hexdigest()[:10]
            sku = f"AUTO-{h}"
            sku_sintetico = True

        prod = session.query(Producto).filter_by(
            proveedor_id=prov.id, sku=sku
        ).first()

        es_nuevo = prod is None
        if es_nuevo:
            prod = Producto(proveedor_id=prov.id, sku=sku, descripcion=desc)
            session.add(prod)

        prod.descripcion = desc
        prod.fob_usd = _to_float(ws.cell(fila, COL_FOB).value)
        prod.medidas = str(ws.cell(fila, COL_MEDIDAS).value or "").strip()
        prod.material = str(ws.cell(fila, COL_MATERIAL).value or "").strip()
        prod.peso_kg = _to_float(ws.cell(fila, COL_PESO).value)
        prod.color = str(ws.cell(fila, COL_COLOR).value or "").strip()
        prod.moq = str(ws.cell(fila, COL_MOQ).value or "").strip()
        prod.packing = str(ws.cell(fila, COL_PACKING).value or "").strip()
        prod.carton_dims = str(ws.cell(fila, COL_CARTON).value or "").strip()
        prod.cbm = _to_float(ws.cell(fila, COL_CBM).value)
        prod.pzas_20ft = _to_int(ws.cell(fila, COL_PZAS20).value)
        prod.pzas_40hq = _to_int(ws.cell(fila, COL_PZAS40).value)
        prod.lead_time = str(ws.cell(fila, COL_LEAD).value or "").strip()

        session.flush()

        if fila in imagenes and es_nuevo:
            data = imagenes[fila]
            ext = ".png"
            if data[:3] == b"\xff\xd8\xff":
                ext = ".jpg"
            nombre_archivo = f"{prov.id}_{prod.id}_{sku or fila}{ext}"
            nombre_archivo = nombre_archivo.replace("/", "_").replace("\\", "_")
            destino = fotos_dir / nombre_archivo
            destino.write_bytes(data)

            foto = Foto(
                producto_id=prod.id,
                ruta_relativa=f"fotos/{nombre_archivo}",
                es_principal=True,
            )
            session.add(foto)

        if es_nuevo:
            nuevos += 1

    return nuevos
