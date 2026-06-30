"""Registro en memoria de jobs de ingest de PDF (procesamiento async).

Motivo: la extraccion de un PDF (Adobe + Claude) puede tardar varios minutos.
Si se corre dentro del request, Cloudflare corta la conexion a los ~100s y
devuelve 524 aunque el backend siga trabajando. La solucion es procesar en un
hilo aparte: el endpoint responde al instante con un job_id y el navegador
consulta el estado por polling (requests cortos que nunca topan el limite).

Store en memoria simple: la app corre en un solo proceso/worker (run-local.bat
arranca uvicorn sin --workers). Si la app se reinicia, los jobs en curso se
pierden -> el status devuelve 404 y el front pide reintentar. Aceptable para uso
interno.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

# Vida maxima de un job terminado antes de purgarse (segundos).
_TTL_TERMINADOS = 60 * 60  # 1 hora

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def crear_job(nombre_archivo: str) -> str:
    """Registra un job nuevo en estado 'procesando' y devuelve su id."""
    job_id = uuid.uuid4().hex
    ahora = time.time()
    with _lock:
        _purgar_viejos_locked(ahora)
        _jobs[job_id] = {
            "estado": "procesando",  # procesando | listo
            "archivo": nombre_archivo,
            "creado": ahora,
            "terminado": None,
            "http_status": None,  # 200 | 422 | 500 cuando estado == listo
            "resultado": None,    # dict que se devuelve al front
        }
    return job_id


def marcar_listo(job_id: str, http_status: int, resultado: dict) -> None:
    """Marca el job como terminado con su http_status logico y payload."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(
            estado="listo",
            terminado=time.time(),
            http_status=http_status,
            resultado=resultado,
        )


def obtener_job(job_id: str) -> dict[str, Any] | None:
    """Devuelve una copia del estado del job, o None si no existe."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        copia = dict(job)
    # segundos transcurridos: util para que el front muestre un cronometro
    base = copia["terminado"] or time.time()
    copia["elapsed"] = round(base - copia["creado"], 1)
    return copia


def _purgar_viejos_locked(ahora: float) -> None:
    """Elimina jobs terminados hace mas de _TTL_TERMINADOS. Asume _lock tomado."""
    muertos = [
        jid for jid, j in _jobs.items()
        if j["terminado"] is not None and (ahora - j["terminado"]) > _TTL_TERMINADOS
    ]
    for jid in muertos:
        _jobs.pop(jid, None)
