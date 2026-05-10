"""
Extraer contenido de un PDF usando Adobe PDF Services Extract API (SDK oficial).

Sube el PDF, espera el procesamiento y descarga el ZIP de resultados con:
- structuredData.json -> estructura del documento (texto, tablas)
- tables/ -> tablas extraidas como xlsx
- figures/ -> imagenes extraidas como png

Uso:
    python extraer_pdf_adobe.py archivo.pdf [carpeta_salida]

Credenciales: lee ADOBE_CLIENT_ID y ADOBE_CLIENT_SECRET desde .env o entorno.
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

from dotenv import load_dotenv

from adobe.pdfservices.operation.auth.service_principal_credentials import (
    ServicePrincipalCredentials,
)
from adobe.pdfservices.operation.pdf_services import PDFServices
from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
from adobe.pdfservices.operation.io.cloud_asset import CloudAsset
from adobe.pdfservices.operation.io.stream_asset import StreamAsset
from adobe.pdfservices.operation.pdfjobs.jobs.extract_pdf_job import ExtractPDFJob
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.extract_element_type import (
    ExtractElementType,
)
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.extract_pdf_params import (
    ExtractPDFParams,
)
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.extract_renditions_element_type import (
    ExtractRenditionsElementType,
)
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.table_structure_type import (
    TableStructureType,
)
from adobe.pdfservices.operation.pdfjobs.result.extract_pdf_result import (
    ExtractPDFResult,
)


def cargar_credenciales() -> tuple[str, str]:
    load_dotenv()
    cid = os.environ.get("ADOBE_CLIENT_ID")
    sec = os.environ.get("ADOBE_CLIENT_SECRET")
    if not cid or not sec:
        sys.exit(
            "Faltan credenciales. Define ADOBE_CLIENT_ID y ADOBE_CLIENT_SECRET "
            "en .env o como variables de entorno."
        )
    return cid, sec


def extraer(pdf_path: str, carpeta_salida: str) -> None:
    cid, sec = cargar_credenciales()

    print("1. Autenticando y abriendo PDF...")
    credentials = ServicePrincipalCredentials(client_id=cid, client_secret=sec)
    pdf_services = PDFServices(credentials=credentials)

    with open(pdf_path, "rb") as f:
        stream_asset = pdf_services.upload(
            input_stream=f.read(),
            mime_type=PDFServicesMediaType.PDF,
        )

    print("2. Lanzando extraccion (texto + tablas + figuras)...")
    extract_params = ExtractPDFParams(
        elements_to_extract=[ExtractElementType.TEXT, ExtractElementType.TABLES],
        elements_to_extract_renditions=[
            ExtractRenditionsElementType.TABLES,
            ExtractRenditionsElementType.FIGURES,
        ],
        table_structure_type=TableStructureType.XLSX,
    )
    job = ExtractPDFJob(input_asset=stream_asset, extract_pdf_params=extract_params)
    location = pdf_services.submit(job)

    print("3. Esperando resultado...")
    pdf_services_response = pdf_services.get_job_result(location, ExtractPDFResult)
    result_asset: CloudAsset = pdf_services_response.get_result().get_resource()
    stream = pdf_services.get_content(result_asset)

    print("4. Descargando ZIP...")
    os.makedirs(carpeta_salida, exist_ok=True)
    zip_path = os.path.join(carpeta_salida, "_resultado.zip")
    with open(zip_path, "wb") as f:
        f.write(stream.get_input_stream())
    print(f"   {zip_path} ({os.path.getsize(zip_path)} bytes)")

    print("5. Descomprimiendo...")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(carpeta_salida)
        nombres = z.namelist()
    print(f"   {len(nombres)} archivos extraidos")

    print()
    print(f"Listo. Resultado en: {carpeta_salida}")
    for raiz, _, archivos in os.walk(carpeta_salida):
        for a in sorted(archivos):
            ruta = os.path.join(raiz, a)
            rel = os.path.relpath(ruta, carpeta_salida)
            print(f"  {rel}  ({os.path.getsize(ruta)} bytes)")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Uso: python extraer_pdf_adobe.py archivo.pdf [carpeta_salida]")

    pdf_path = os.path.abspath(sys.argv[1])
    if not os.path.exists(pdf_path):
        sys.exit(f"No existe el archivo: {pdf_path}")

    if len(sys.argv) >= 3:
        carpeta_salida = os.path.abspath(sys.argv[2])
    else:
        base = Path(pdf_path).stem.replace(" ", "_")[:60]
        carpeta_salida = os.path.join(
            Path(pdf_path).parent, f"_adobe_extract_{base}"
        )

    print(f"Origen:  {pdf_path}")
    print(f"Destino: {carpeta_salida}")
    print()

    extraer(pdf_path, carpeta_salida)


if __name__ == "__main__":
    main()
