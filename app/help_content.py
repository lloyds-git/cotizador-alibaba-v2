"""Carga del contenido de Ayuda (manual de usuario) desde archivos Markdown.

Los .md viven en app/help/ con prefijo numerico para orden determinista
(00-..., 01-..., etc). El frontend (pagina /ayuda y modal "?") consume esto
via GET /api/ayuda y lo renderiza con marked.js. Una sola fuente de verdad.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

HELP_DIR = Path(__file__).parent / "help"

# Cache en memoria de proceso. Con uvicorn --reload guardar un .md reinicia el
# proceso y refresca el cache. Sin reload, usar invalidar_cache() o reiniciar.
_cache: list[dict] | None = None

# "00-introduccion.md" -> "introduccion"
_PREFIJO = re.compile(r"^\d+[-_]?")


def _slug(nombre_archivo: str) -> str:
    base = Path(nombre_archivo).stem
    return _PREFIJO.sub("", base)


def _titulo(md_text: str, fallback: str) -> str:
    """Primer encabezado '# ...' del Markdown; si no hay, el slug capitalizado."""
    for linea in md_text.splitlines():
        s = linea.strip()
        if s.startswith("# "):
            return s[2:].strip()
        if s:  # primera linea no vacia que no es H1 -> usar fallback
            break
    return fallback.replace("-", " ").replace("_", " ").capitalize()


def cargar_ayuda(force: bool = False) -> list[dict]:
    """Devuelve [{id, titulo, markdown}] ordenado por nombre de archivo.

    Defensivo: si falta la carpeta devuelve []; si un archivo no se puede leer
    lo salta (log warning) sin abortar el resto.
    """
    global _cache
    if _cache is not None and not force:
        return _cache

    secciones: list[dict] = []
    if not HELP_DIR.exists():
        logger.warning("Carpeta de ayuda no encontrada: %s", HELP_DIR)
        _cache = []
        return _cache

    for archivo in sorted(HELP_DIR.glob("*.md"), key=lambda p: p.name):
        try:
            texto = archivo.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("No se pudo leer el archivo de ayuda %s: %s", archivo, e)
            continue
        slug = _slug(archivo.name)
        secciones.append(
            {
                "id": slug,
                "titulo": _titulo(texto, slug),
                "markdown": texto,
            }
        )

    _cache = secciones
    return _cache


def invalidar_cache() -> None:
    """Fuerza recargar los .md en la proxima llamada a cargar_ayuda()."""
    global _cache
    _cache = None
