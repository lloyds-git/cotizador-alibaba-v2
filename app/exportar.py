"""
Genera el xlsx intermedio a partir de los productos marcados en la BD.
Despues, ese xlsx se puede pasar a llenar_formato_hd.py para producir
el formato HD final.
"""

from pathlib import Path

import openpyxl
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Font

from sqlalchemy.orm import Session

from app.modelos import Producto


def generar_formato_hd_desde_marcados(
    session: Session,
    xlsx_intermedio: str,
    base_fotos: str,
) -> int:
    """
    Construye un xlsx intermedio (mismo layout que pdf_a_formato_hd.py)
    con todos los productos marcados_cotizar=True.

    Devuelve la cantidad de productos exportados.
    """
    productos = (
        session.query(Producto)
        .filter(Producto.marcado_cotizar.is_(True))
        .all()
    )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cotizacion seleccionados"

    headers = ["Foto", "SKU", "Descripcion", "Medidas", "Material", "Peso (kg)",
               "Color", "MOQ", "Packing", "Carton dims", "CBM",
               "Pzas 20ft", "Pzas 40hq", "Lead time", "FOB USD"]
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = Font(bold=True)
    ws.row_dimensions[1].height = 25

    for i, p in enumerate(productos, start=2):
        ws.row_dimensions[i].height = 90
        ws.cell(i, 2, value=p.sku or "")
        ws.cell(i, 3, value=p.descripcion or "")
        ws.cell(i, 4, value=p.medidas or "")
        ws.cell(i, 5, value=p.material or "")
        ws.cell(i, 6, value=p.peso_kg)
        ws.cell(i, 7, value=p.color or "")
        ws.cell(i, 8, value=p.moq or "")
        ws.cell(i, 9, value=p.packing or "")
        ws.cell(i, 10, value=p.carton_dims or "")
        ws.cell(i, 11, value=p.cbm)
        ws.cell(i, 12, value=p.pzas_20ft)
        ws.cell(i, 13, value=p.pzas_40hq)
        ws.cell(i, 14, value=p.lead_time or "")
        ws.cell(i, 15, value=p.fob_usd)

        if p.fotos:
            foto_path = Path(base_fotos) / p.fotos[0].ruta_relativa
            if foto_path.exists():
                try:
                    img = XLImage(str(foto_path))
                    img.width = min(img.width, 120)
                    img.height = min(img.height, 120)
                    img.anchor = f"A{i}"
                    ws.add_image(img)
                except Exception:
                    pass

    Path(xlsx_intermedio).parent.mkdir(parents=True, exist_ok=True)
    wb.save(xlsx_intermedio)
    return len(productos)
