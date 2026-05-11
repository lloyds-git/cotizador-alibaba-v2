"""
Llenar el formato HD-Mascotas con datos de un archivo de cotizacion.

Uso:
    python llenar_formato_hd.py ArchivoXXX.xlsx "Formato HD-Mascotas.xlsb"
    python llenar_formato_hd.py            (modo interactivo)

El script usa Excel via COM (pywin32), por lo que funciona con .xlsx y .xlsb,
copia imagenes incrustadas conservando el formato y respeta los estilos del template.

Mapeo origen (filas) -> destino (columnas C, D, E, ...):
    columna A (foto)  -> fila 2  (imagen incrustada, si existe)
    "Totikay Pets..." -> fila 3
    "TBD"             -> fila 4
    "TBD"             -> fila 5
    "PRIMARY"         -> fila 6
    "TBD"             -> fila 7
    columna C         -> fila 8
    "MPT-0001"...     -> fila 9
    "China"           -> fila 10
    columna O         -> fila 11
    "NA"              -> filas 12, 13, 14, 15
    columna U         -> fila 16
    columna V         -> fila 17
    "NA"              -> fila 18

El nombre de salida se construye como: formato-hd-<nombre_origen_minusculas>.xlsx
y se guarda en la misma carpeta que el archivo origen.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import win32com.client
import pythoncom

# Constantes de Excel
XL_OPENXML = 51  # xlsx
XL_NONE = -4142
MSO_TRUE = -1
MSO_FALSE = 0
XL_HALIGN_CENTER = -4108  # xlCenter
XL_VALIGN_CENTER = -4108  # xlCenter

# 1 pixel = 0.75 puntos (a 96 DPI)
PIXELES_A_PUNTOS = 0.75

CONSTANTES = {
    3: "Totikay Pets SA de CV",
    4: "TBD",
    5: "TBD",
    6: "PRIMARY",
    7: "TBD",
    10: "China",
    12: "NA",
    13: "NA",
    14: "NA",
    15: "NA",
    18: "NA",
}

# Mapeo por defecto: columna del origen (numero) -> fila del destino
MAPEO_DEFAULT = {
    3: 8,    # C -> fila 8
    15: 11,  # O -> fila 11
    21: 16,  # U -> fila 16
    22: 17,  # V -> fila 17
}


def col_letra_a_num(letra: str) -> int:
    """Convierte 'A' -> 1, 'Z' -> 26, 'AA' -> 27, etc."""
    letra = letra.strip().upper()
    n = 0
    for ch in letra:
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"Letra de columna invalida: {letra!r}")
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def parsear_mapeo(s: str) -> dict[int, int]:
    """
    Parsea un mapeo del tipo 'C=8,O=11,U=16,V=17' o 'C=8,S=11,Y=16,Z=17'
    a un dict {col_num: fila_dest}.
    """
    resultado: dict[int, int] = {}
    for parte in s.split(","):
        parte = parte.strip()
        if not parte:
            continue
        if "=" not in parte:
            raise ValueError(f"Mapeo invalido (falta '='): {parte!r}")
        col, fila = parte.split("=", 1)
        resultado[col_letra_a_num(col)] = int(fila.strip())
    return resultado


def pedir_ruta(prompt: str) -> str:
    ruta = input(prompt).strip().strip('"').strip("'")
    if not ruta:
        sys.exit("Cancelado: no se proporciono ruta.")
    if not os.path.exists(ruta):
        sys.exit(f"No existe el archivo: {ruta}")
    return os.path.abspath(ruta)


def confirmar_sobreescritura(ruta: str) -> None:
    if os.path.exists(ruta):
        resp = input(f"El archivo '{ruta}' ya existe. Sobrescribir? (s/N): ").strip().lower()
        if resp not in ("s", "si", "sí", "y", "yes"):
            sys.exit("Cancelado por el usuario.")
        try:
            os.remove(ruta)
        except PermissionError:
            sys.exit(f"No se pudo borrar el archivo existente (esta abierto?): {ruta}")


def detectar_n_productos(
    ws_origen, filas_con_imagen: set[int], cols_relevantes: list[int]
) -> int:
    """Cuenta filas con datos a partir de la fila 2, parando en la primera vacia."""
    n = 0
    fila = 2
    max_iter = 10000
    while fila < max_iter:
        tiene_foto = fila in filas_con_imagen
        tiene_valor = any(
            ws_origen.Cells(fila, c).Value not in (None, "")
            for c in cols_relevantes if c != 1
        )
        if not tiene_foto and not tiene_valor:
            break
        n += 1
        fila += 1
    return n


def construir_nombre_salida(archivo_origen: str) -> str:
    base = Path(archivo_origen).stem.lower()
    # Si viene de pipeline PDF, el origen empieza con "_intermedio_"; quitarlo
    if base.startswith("_intermedio_"):
        base = base[len("_intermedio_"):]
    carpeta = Path(archivo_origen).parent
    return str(carpeta / f"formato-hd-{base}.xlsx")


def extraer_imagenes_drawing(archivo_xlsx: str, tmpdir: str) -> dict[int, str]:
    """
    Extrae las imagenes flotantes (Pictures/Shapes) de la columna A leyendo
    directamente xl/drawings/drawing1.xml + xl/drawings/_rels/drawing1.xml.rels
    + xl/media/. Mas confiable que Chart.Export via COM.

    Devuelve {fila: ruta_a_imagen_extraida}.
    Notas:
    - Las anclas oneCellAnchor y twoCellAnchor se procesan igual.
    - Solo se consideran imagenes ancladas en col=0 (columna A).
    - Si dos imagenes coinciden en la misma fila, se queda la primera.
    """
    if not archivo_xlsx.lower().endswith(".xlsx"):
        return {}

    resultado: dict[int, str] = {}
    try:
        with zipfile.ZipFile(archivo_xlsx) as z:
            nombres = set(z.namelist())
            if "xl/drawings/drawing1.xml" not in nombres:
                return {}
            if "xl/drawings/_rels/drawing1.xml.rels" not in nombres:
                return {}

            rels_xml = z.read("xl/drawings/_rels/drawing1.xml.rels").decode("utf-8")
            ns_rels = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
            rels_root = ET.fromstring(rels_xml)
            rid_a_target: dict[str, str] = {}
            for rel in rels_root.findall("r:Relationship", ns_rels):
                rid = rel.get("Id")
                target = rel.get("Target")
                if rid and target:
                    rid_a_target[rid] = target

            drawing_xml = z.read("xl/drawings/drawing1.xml").decode("utf-8")
            ns = {
                "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
                "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
                "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            }
            root = ET.fromstring(drawing_xml)

            anchors = []
            for tag in ("oneCellAnchor", "twoCellAnchor", "absoluteAnchor"):
                anchors.extend(root.findall(f"xdr:{tag}", ns))

            for anchor in anchors:
                pic = anchor.find("xdr:pic", ns)
                if pic is None:
                    continue
                from_node = anchor.find("xdr:from", ns)
                if from_node is None:
                    continue
                col_node = from_node.find("xdr:col", ns)
                row_node = from_node.find("xdr:row", ns)
                if col_node is None or row_node is None:
                    continue
                try:
                    col_0 = int(col_node.text)
                    row_0 = int(row_node.text)
                except (TypeError, ValueError):
                    continue
                if col_0 != 0:  # solo columna A
                    continue
                fila_excel = row_0 + 1  # 0-indexado a 1-indexado
                if fila_excel in resultado:
                    continue

                blip = pic.find("xdr:blipFill/a:blip", ns)
                if blip is None:
                    continue
                rid = blip.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
                )
                if not rid:
                    continue
                target = rid_a_target.get(rid)
                if not target:
                    continue
                # target puede ser ruta relativa "../media/image1.png" (Office)
                # o ruta absoluta "/xl/media/image1.png" (openpyxl). Normalizar.
                if target.startswith("/"):
                    ruta_zip = target.lstrip("/")
                else:
                    ruta_zip = os.path.normpath(
                        os.path.join("xl/drawings", target)
                    ).replace("\\", "/")
                if ruta_zip not in nombres:
                    continue
                ext = os.path.splitext(ruta_zip)[1] or ".png"
                ruta_local = os.path.join(tmpdir, f"flotante_fila{fila_excel}{ext}")
                with z.open(ruta_zip) as src, open(ruta_local, "wb") as dst:
                    dst.write(src.read())
                resultado[fila_excel] = ruta_local
    except Exception as e:
        print(f"  Aviso: no se pudieron extraer imagenes flotantes via ZIP ({e})")
        return {}
    return resultado


def extraer_imagenes_in_cell(archivo_xlsx: str, tmpdir: str) -> dict[int, str]:
    """
    Extrae las imagenes 'in-cell' (Excel 365 'Image in Cell') de la columna A
    del archivo xlsx. Devuelve un dict {fila: ruta_a_png_extraido}.

    Las in-cell pictures NO son visibles via COM (Pictures()/Shapes), pero se
    almacenan en la estructura interna del xlsx en xl/richData/ + xl/media/.
    Mapeo: celda(vm) -> valueMetadata -> richValueRel -> imagen.
    """
    if not archivo_xlsx.lower().endswith(".xlsx"):
        return {}

    resultado: dict[int, str] = {}
    try:
        with zipfile.ZipFile(archivo_xlsx) as z:
            nombres = set(z.namelist())
            requeridos = {
                "xl/worksheets/sheet1.xml",
                "xl/metadata.xml",
                "xl/richData/richValueRel.xml",
                "xl/richData/_rels/richValueRel.xml.rels",
            }
            if not requeridos.issubset(nombres):
                return {}

            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
            # celdas con vm en columna A
            celda_a_vm: dict[int, int] = {}
            for ref, vm in re.findall(
                r'<c[^>]*r="([A-Z]+\d+)"[^>]*vm="(\d+)"', sheet
            ):
                if ref.startswith("A") and ref[1:].isdigit():
                    celda_a_vm[int(ref[1:])] = int(vm)
            if not celda_a_vm:
                return {}

            # metadata.xml: valueMetadata -> indice de richValue
            meta_xml = z.read("xl/metadata.xml").decode("utf-8")
            ns_meta = {
                "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
            }
            root = ET.fromstring(meta_xml)
            value_meta = root.find("x:valueMetadata", ns_meta)
            vm_a_rvb: dict[int, int] = {}
            if value_meta is not None:
                # cada <bk> en orden -> vm = (indice + 1)
                for idx_vm, bk in enumerate(value_meta.findall("x:bk", ns_meta), start=1):
                    rc = bk.find("x:rc", ns_meta)
                    if rc is not None:
                        # v=0 indexed indice de richValue
                        v = rc.get("v")
                        if v is not None:
                            vm_a_rvb[idx_vm] = int(v)

            # richValueRel.xml: rvb i=N -> rId
            rvb_xml = z.read("xl/richData/richValueRel.xml").decode("utf-8")
            ns_rvb = {
                "rvr": "http://schemas.microsoft.com/office/spreadsheetml/2022/richvaluerel",
                "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            }
            rvb_root = ET.fromstring(rvb_xml)
            rvb_a_rid: dict[int, str] = {}
            for idx, rel in enumerate(rvb_root.findall("rvr:rel", ns_rvb)):
                rid = rel.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                if rid:
                    rvb_a_rid[idx] = rid

            # richValueRel.xml.rels: rId -> Target (path a media/imageX)
            rels_xml = z.read("xl/richData/_rels/richValueRel.xml.rels").decode("utf-8")
            ns_rels = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
            rels_root = ET.fromstring(rels_xml)
            rid_a_target: dict[str, str] = {}
            for rel in rels_root.findall("r:Relationship", ns_rels):
                rid = rel.get("Id")
                target = rel.get("Target")
                if rid and target:
                    rid_a_target[rid] = target

            # Extraer cada imagen al tmpdir
            for fila, vm in celda_a_vm.items():
                rvb = vm_a_rvb.get(vm)
                if rvb is None:
                    continue
                rid = rvb_a_rid.get(rvb)
                if rid is None:
                    continue
                target = rid_a_target.get(rid)
                if target is None:
                    continue
                # target puede ser relativo "../media/..." o absoluto "/xl/media/..."
                if target.startswith("/"):
                    ruta_zip = target.lstrip("/")
                else:
                    ruta_zip = os.path.normpath(
                        os.path.join("xl/richData", target)
                    ).replace("\\", "/")
                if ruta_zip not in nombres:
                    continue
                ext = os.path.splitext(ruta_zip)[1] or ".png"
                ruta_local = os.path.join(tmpdir, f"incell_fila{fila}{ext}")
                with z.open(ruta_zip) as src, open(ruta_local, "wb") as dst:
                    dst.write(src.read())
                resultado[fila] = ruta_local
    except Exception as e:
        print(f"  Aviso: no se pudieron extraer imagenes in-cell ({e})")
        return {}

    return resultado


def exportar_imagen_a_archivo(excel, ws_origen, pic, ruta_png: str) -> bool:
    """
    Exporta una Picture de Excel a un archivo PNG.
    Tecnica: copiar la imagen a un Chart temporal y exportar el chart como imagen.
    """
    try:
        ancho = pic.Width
        alto = pic.Height
        # Crear un chart temporal del tamano de la imagen
        chart_obj = ws_origen.ChartObjects().Add(0, 0, ancho, alto)
        try:
            pic.Copy()
            chart_obj.Chart.Paste()
            chart_obj.Chart.Export(Filename=ruta_png, FilterName="PNG")
            return os.path.exists(ruta_png) and os.path.getsize(ruta_png) > 0
        finally:
            chart_obj.Delete()
    except Exception as e:
        print(f"  Error exportando imagen: {e}")
        return False


def llenar_formato(
    archivo_origen: str,
    archivo_formato: str,
    archivo_salida: str,
    mapeo_datos: dict[int, int],
) -> int:
    pythoncom.CoInitialize()
    excel = win32com.client.DispatchEx("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False
    excel.ScreenUpdating = False

    wb_origen = None
    wb_dest = None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Extraer todas las imagenes del archivo origen leyendo el ZIP del xlsx
            # directamente. Esto es mas confiable que el chart-hack via COM y
            # cubre tanto Pictures flotantes como "in-cell pictures".
            in_cell_fotos = extraer_imagenes_in_cell(archivo_origen, tmpdir)
            flotantes_fotos = extraer_imagenes_drawing(archivo_origen, tmpdir)
            # Combinar: in-cell tiene preferencia si hubiera duplicado
            todas_fotos: dict[int, str] = {**flotantes_fotos, **in_cell_fotos}

            if flotantes_fotos:
                print(f"  Detectadas {len(flotantes_fotos)} imagenes flotantes: filas {sorted(flotantes_fotos)}")
            if in_cell_fotos:
                print(f"  Detectadas {len(in_cell_fotos)} imagenes in-cell: filas {sorted(in_cell_fotos)}")

            wb_origen = excel.Workbooks.Open(archivo_origen, ReadOnly=True)
            ws_origen = wb_origen.Worksheets(1)

            wb_dest = excel.Workbooks.Open(archivo_formato)
            ws_dest = wb_dest.Worksheets(1)

            filas_con_imagen = set(todas_fotos.keys())

            cols_relevantes = [1] + list(mapeo_datos.keys())
            n = detectar_n_productos(ws_origen, filas_con_imagen, cols_relevantes)
            if n == 0:
                sys.exit("El archivo origen no tiene productos detectables a partir de la fila 2.")
            print(f"Detectados {n} productos en el origen.")

            # Ancho fijo de columna y alto de fila 2 para todas las columnas
            # de productos. ColumnWidth se mide en "caracteres" (depende de la
            # fuente del workbook), asi que calibramos con la columna C primero:
            # establecemos un valor, leemos los puntos resultantes y derivamos
            # el factor real chars-por-pt para esta plantilla.
            ANCHO_COL_PX = 270
            ALTO_FILA2_PX = 190
            ancho_col_pt_objetivo = ANCHO_COL_PX * PIXELES_A_PUNTOS
            alto_fila2_pt = ALTO_FILA2_PX * PIXELES_A_PUNTOS

            # Calibracion: setear C a 10 chars y medir
            ws_dest.Columns(3).ColumnWidth = 10
            pt_para_10_chars = ws_dest.Columns(3).Width
            chars_por_pt = 10 / pt_para_10_chars if pt_para_10_chars else 0.14
            ancho_col_chars = ancho_col_pt_objetivo * chars_por_pt

            # Aplicar el ancho objetivo a TODAS las columnas de productos AHORA,
            # antes del bucle, para que las posiciones (Cells.Left) sean estables
            # cuando insertemos imagenes.
            for c in range(3, 3 + n):
                ws_dest.Columns(c).ColumnWidth = ancho_col_chars

            ws_dest.Rows(2).RowHeight = alto_fila2_pt

            for i in range(n):
                fila_origen = 2 + i
                col_dest = 3 + i  # C=3, D=4, ...

                # Constantes
                for fila_dest, valor in CONSTANTES.items():
                    ws_dest.Cells(fila_dest, col_dest).Value = valor

                # MPT-XXXX (autoincremental empezando en 0001)
                ws_dest.Cells(9, col_dest).Value = f"MPT-{i + 1:04d}"

                # Datos del origen
                for col_origen_idx, fila_dest in mapeo_datos.items():
                    valor = ws_origen.Cells(fila_origen, col_origen_idx).Value
                    ws_dest.Cells(fila_dest, col_dest).Value = valor

                # Resolver imagen del origen (extraida desde el ZIP)
                ruta_png = todas_fotos.get(fila_origen)

                if ruta_png:
                    celda_destino = ws_dest.Cells(2, col_dest)
                    alto_fila_pt = ws_dest.Rows(2).RowHeight
                    ancho_col_pt = ws_dest.Columns(col_dest).Width
                    # Margen interno para evitar que el redondeo de Excel
                    # ancle la imagen a la columna anterior.
                    MARGEN_PT = 1.0
                    area_w = ancho_col_pt - 2 * MARGEN_PT
                    area_h = alto_fila_pt - 2 * MARGEN_PT
                    shape = ws_dest.Shapes.AddPicture(
                        Filename=ruta_png,
                        LinkToFile=MSO_FALSE,
                        SaveWithDocument=MSO_TRUE,
                        Left=celda_destino.Left + MARGEN_PT,
                        Top=celda_destino.Top + MARGEN_PT,
                        Width=area_w,
                        Height=area_h,
                    )
                    shape.LockAspectRatio = MSO_TRUE
                    # Reescalar manteniendo aspecto original
                    try:
                        from PIL import Image as PILImage
                        with PILImage.open(ruta_png) as im:
                            iw, ih = im.size
                        ratio = min(area_w / iw, area_h / ih)
                        shape.Width = iw * ratio
                        shape.Height = ih * ratio
                    except Exception:
                        pass
                    # Centrar en la celda dejando un minimo de margen para que
                    # Excel no ancle el shape a la columna anterior por redondeo
                    offset_x = max(MARGEN_PT, (ancho_col_pt - shape.Width) / 2)
                    offset_y = max(MARGEN_PT, (alto_fila_pt - shape.Height) / 2)
                    shape.Left = celda_destino.Left + offset_x
                    shape.Top = celda_destino.Top + offset_y

            # Formato de filas y columnas de productos
            col_inicial = 3  # C
            col_final = 3 + n - 1
            rango_cols = ws_dest.Range(
                ws_dest.Cells(1, col_inicial),
                ws_dest.Cells(20, col_final),
            )

            # Centrado horizontal y vertical en todo el rango de productos
            rango_cols.HorizontalAlignment = XL_HALIGN_CENTER
            rango_cols.VerticalAlignment = XL_VALIGN_CENTER

            # Fila 8 (descripcion): alto 190px, wrap text
            ws_dest.Rows(8).RowHeight = 190 * PIXELES_A_PUNTOS
            ws_dest.Range(
                ws_dest.Cells(8, col_inicial),
                ws_dest.Cells(8, col_final),
            ).WrapText = True

            # Fila 11 (Domestic Cost): formato $
            ws_dest.Range(
                ws_dest.Cells(11, col_inicial),
                ws_dest.Cells(11, col_final),
            ).NumberFormat = "$#,##0.00"

            # Fila 16 (Retail/Retail Sugerido): formato $
            ws_dest.Range(
                ws_dest.Cells(16, col_inicial),
                ws_dest.Cells(16, col_final),
            ).NumberFormat = "$#,##0.00"

            # Fila 17 (THD Margin): formato % con 2 decimales
            ws_dest.Range(
                ws_dest.Cells(17, col_inicial),
                ws_dest.Cells(17, col_final),
            ).NumberFormat = "0.00%"

        # Guardar como xlsx
        wb_dest.SaveAs(archivo_salida, FileFormat=XL_OPENXML)
        return n
    finally:
        try:
            if wb_origen is not None:
                wb_origen.Close(SaveChanges=False)
        except Exception:
            pass
        try:
            if wb_dest is not None:
                wb_dest.Close(SaveChanges=False)
        except Exception:
            pass
        try:
            excel.ScreenUpdating = True
            excel.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()


def main() -> None:
    # Extraer --mapeo "C=8,O=11,U=16,V=17" si esta presente en argv
    args = list(sys.argv[1:])
    mapeo_str = None
    # Flag --yes para saltar confirmacion de sobreescritura (uso desde subprocess)
    auto_yes = False
    if "--yes" in args:
        args.remove("--yes")
        auto_yes = True
    if "--mapeo" in args:
        idx = args.index("--mapeo")
        if idx + 1 >= len(args):
            sys.exit("Falta el valor despues de --mapeo")
        mapeo_str = args[idx + 1]
        del args[idx:idx + 2]
    elif os.environ.get("MAPEO"):
        mapeo_str = os.environ["MAPEO"]

    if mapeo_str:
        try:
            mapeo_datos = parsear_mapeo(mapeo_str)
        except Exception as e:
            sys.exit(f"Mapeo invalido: {e}")
    else:
        mapeo_datos = MAPEO_DEFAULT

    if len(args) >= 2:
        archivo_origen = os.path.abspath(args[0])
        archivo_formato = os.path.abspath(args[1])
    else:
        print("Modo interactivo (no se pasaron argumentos).")
        archivo_origen = pedir_ruta("Ruta del archivo origen: ")
        archivo_formato = pedir_ruta("Ruta del formato HD: ")

    if not os.path.exists(archivo_origen):
        sys.exit(f"No existe el archivo origen: {archivo_origen}")
    if not os.path.exists(archivo_formato):
        sys.exit(f"No existe el archivo formato: {archivo_formato}")

    archivo_salida = construir_nombre_salida(archivo_origen)
    if auto_yes:
        # Borrar sin preguntar (uso desde subprocess: la app ya valido permisos)
        if os.path.exists(archivo_salida):
            try:
                os.remove(archivo_salida)
            except PermissionError:
                sys.exit(f"No se pudo borrar el archivo existente (esta abierto?): {archivo_salida}")
    else:
        confirmar_sobreescritura(archivo_salida)

    print(f"Mapeo aplicado: {mapeo_datos}")
    n = llenar_formato(archivo_origen, archivo_formato, archivo_salida, mapeo_datos)
    print(f"Listo. {n} productos escritos en: {archivo_salida}")


if __name__ == "__main__":
    main()
