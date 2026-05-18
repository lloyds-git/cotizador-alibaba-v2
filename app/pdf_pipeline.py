"""Pipeline reusable: PDF nuevo -> intermedio xlsx -> BD.

Encapsula el flujo que antes solo existia en `cmd_pdf` del CLI para que el
endpoint web POST /api/ingest/pdf pueda invocarlo sin duplicar logica
(extraccion via pdf_a_formato_hd.py + ingest + gate de calidad + rollback).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from app.calidad import calcular_veredicto
from app.ingest import (
    buscar_proveedor_existente,
    ingestar_xlsx_intermedio,
    resolver_nombre_proveedor,
)
from app.modelos import Proveedor, Producto


PROYECTO_ROOT = Path(__file__).parent.parent
PDF_A_FORMATO_HD = PROYECTO_ROOT / "pdf_a_formato_hd.py"


class PdfPipelineError(RuntimeError):
    """Falla controlada en el pipeline. Mensaje listo para mostrar al usuario."""


def procesar_pdf(
    session: Session,
    pdf_path: Path,
    *,
    fotos_destino: Path,
    forzar_calidad: bool = False,
) -> dict:
    """Procesa un PDF y lo ingesta a la BD.

    Pasos:
      1. Ejecuta pdf_a_formato_hd.py como subprocess. Genera _intermedio_*.xlsx
         y _intermedio_*.xlsx.meta.json en la misma carpeta del PDF.
      2. Toma el _intermedio_*.xlsx mas reciente de esa carpeta.
      3. Llama ingestar_xlsx_intermedio. La transaccion queda abierta.
      4. session.flush() y corre el gate de calidad sobre los productos del
         proveedor recien tocado.
      5. Si veredicto == "REINGESTAR" y forzar_calidad=False: rollback +
         limpieza de fotos fisicas + devuelve dict con ok=False.
      6. Si veredicto OK/REVISAR (o forzar_calidad=True): commit + devuelve
         dict con ok=True y stats.

    Lanza PdfPipelineError con mensaje claro si falla la extraccion o no
    aparece intermedio.
    """
    if not PDF_A_FORMATO_HD.exists():
        raise PdfPipelineError(f"No existe el script: {PDF_A_FORMATO_HD}")
    if not pdf_path.exists():
        raise PdfPipelineError(f"PDF no existe: {pdf_path}")

    # Snapshot de fotos fisicas pre-ingest para limpieza en rollback.
    fotos_antes = set(fotos_destino.glob("*")) if fotos_destino.exists() else set()

    # Paso 1: extraccion. cwd=PROYECTO_ROOT para que pdf_a_formato_hd.py
    # encuentre _adobe_extract/figures/tables relativos al proyecto.
    #
    # --claude forzado: por default pdf_a_formato_hd solo dispara Claude si
    # el parser heuristico mide <40% de calidad, pero esa metrica solo cuenta
    # filas con FOB y no valida que se hayan detectado SKUs ni seller. PDFs
    # con muchas filas (incluida basura tipo "Payment Sub-Total", "Issued by")
    # pueden quedar sobre el umbral sin que el parser heuristico haya extraido
    # nada util. Pasar --claude garantiza que siempre intentamos LLM extract
    # (~$0.005-$0.02 por PDF) -- aceptable para uso interno de cotizaciones y
    # previene el caso de proveedor falso por seller=None.
    result = subprocess.run(
        [sys.executable, str(PDF_A_FORMATO_HD), str(pdf_path), "--claude"],
        cwd=str(PROYECTO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Mostramos las ultimas lineas de stderr para diagnostico
        cola = "\n".join((result.stderr or "").splitlines()[-15:])
        raise PdfPipelineError(
            f"Fallo pdf_a_formato_hd (exit {result.returncode}). Ultimas lineas:\n{cola}"
        )

    # stdout del subprocess: lo guardamos para devolverlo como diagnostico
    # cuando el resultado final sea VACIO/REINGESTAR (asi el usuario ve por
    # que fallo sin tener que pedir logs).
    stdout_pipeline = result.stdout or ""

    # Paso 2: localizar el intermedio. Queda junto al PDF (mismo dir).
    carpeta = pdf_path.parent
    intermedios = sorted(
        carpeta.glob("_intermedio_*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not intermedios:
        raise PdfPipelineError(
            f"No se genero _intermedio_*.xlsx en {carpeta}. "
            "Revisa los logs de pdf_a_formato_hd."
        )
    xlsx_intermedio = intermedios[0]

    # Paso 3: ingest. resolver_nombre_proveedor lee .meta.json si existe.
    nombre_prov = resolver_nombre_proveedor(str(xlsx_intermedio))

    try:
        n_nuevos = ingestar_xlsx_intermedio(
            session=session,
            xlsx_path=str(xlsx_intermedio),
            nombre_proveedor=None,  # lo resuelve ingest desde .meta.json
            fotos_destino=str(fotos_destino),
        )
    except Exception as e:
        session.rollback()
        _limpiar_fotos_nuevas(fotos_destino, fotos_antes)
        raise PdfPipelineError(f"Fallo ingest_xlsx_intermedio: {e}") from e

    # Paso 4: flush para tener IDs y poder consultar el proveedor.
    # Usamos buscar_proveedor_existente (con normalizacion) en caso de que
    # el ingest haya consolidado a un proveedor preexistente con nombre
    # ligeramente distinto (ej. con/sin LTD); si buscaramos por nombre exacto
    # nos daria None y el gate reportaria VACIO incorrectamente.
    session.flush()
    prov = buscar_proveedor_existente(session, nombre_prov)
    prods = (
        session.query(Producto).filter_by(proveedor_id=prov.id).all()
        if prov else []
    )
    info = calcular_veredicto(prods)

    # Paso 5a: VACIO => no hay nada que forzar, siempre rollback.
    if info["veredicto"] == "VACIO":
        session.rollback()
        _limpiar_fotos_nuevas(fotos_destino, fotos_antes)
        return {
            "ok": False,
            "veredicto": "VACIO",
            "motivo": info["motivo"],
            "puede_forzar": False,
            "proveedor": nombre_prov,
            "intermedio": xlsx_intermedio.name,
            "n_evaluados": 0,
            "log": _cola_relevante(stdout_pipeline),
        }

    # Paso 5b: REINGESTAR + no forzado => rollback y limpieza.
    if info["veredicto"] == "REINGESTAR" and not forzar_calidad:
        session.rollback()
        _limpiar_fotos_nuevas(fotos_destino, fotos_antes)
        return {
            "ok": False,
            "veredicto": "REINGESTAR",
            "motivo": info["motivo"],
            "puede_forzar": True,
            "proveedor": nombre_prov,
            "intermedio": xlsx_intermedio.name,
            "n_evaluados": info["n"],
            "log": _cola_relevante(stdout_pipeline),
        }

    # Paso 6: commit. Veredicto OK, REVISAR o forzado.
    session.commit()
    return {
        "ok": True,
        "veredicto": info["veredicto"],
        "motivo": info["motivo"],
        "proveedor": nombre_prov,
        "productos_nuevos": n_nuevos,
        "productos_totales_proveedor": info["n"],
        "intermedio": xlsx_intermedio.name,
        "forzado": forzar_calidad and info["veredicto"] == "REINGESTAR",
    }


def _cola_relevante(texto: str, max_lineas: int = 25) -> str:
    """Devuelve las ultimas N lineas no vacias del texto, para diagnostico
    en la UI. Filtra rutas absolutas del sistema (ruido visual)."""
    if not texto:
        return ""
    lineas = [l.rstrip() for l in texto.splitlines() if l.strip()]
    return "\n".join(lineas[-max_lineas:])


def _limpiar_fotos_nuevas(fotos_destino: Path, fotos_antes: set[Path]) -> int:
    """Borra las fotos creadas durante esta corrida (rollback fisico)."""
    if not fotos_destino.exists():
        return 0
    fotos_ahora = set(fotos_destino.glob("*"))
    nuevas = fotos_ahora - fotos_antes
    borradas = 0
    for f in nuevas:
        try:
            f.unlink()
            borradas += 1
        except OSError:
            pass
    return borradas
