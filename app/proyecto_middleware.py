"""Middleware que exige un proyecto activo en la sesion.

Analogo a AuthMiddleware pero para el proyecto: una vez logueado, el usuario
debe tener un proyecto seleccionado en session['proyecto']. Si no lo tiene (o
apunta a un proyecto inexistente), redirige a /proyectos (GET HTML) o responde
400 (API / metodos no-GET).

Se ejecuta DESPUES de AuthMiddleware (ver orden en main.crear_app): si no hay
usuario logueado, deja pasar y AuthMiddleware maneja la redireccion a /login.

Tests: se bypassea con AUTH_DISABLED=1 (igual que AuthMiddleware); ademas los
tests sustituyen get_db via dependency_overrides, asi que no dependen del
proyecto en sesion.
"""
from __future__ import annotations

import os

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app import db as db_module
from app.auth import PUBLIC_PREFIXES

# Rutas exentas de exigir proyecto: las publicas (login/auth/fotos/...) + la
# gestion de proyectos (donde justamente se crea/selecciona uno).
EXENTAS = PUBLIC_PREFIXES + ("/proyectos", "/api/proyectos")


def _es_exenta(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in EXENTAS)


def _proyecto_valido(slug: str | None) -> bool:
    """True si el slug es valido y su BD existe en disco."""
    if not db_module.slug_valido(slug):
        return False
    try:
        return db_module.ruta_bd_proyecto(slug).exists()
    except ValueError:
        return False


class ProyectoMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if os.environ.get("AUTH_DISABLED") == "1":
            return await call_next(request)

        path = request.url.path
        if _es_exenta(path):
            return await call_next(request)

        # Sin usuario logueado -> lo maneja AuthMiddleware (ya corrio antes).
        if not request.session.get("user"):
            return await call_next(request)

        if _proyecto_valido(request.session.get("proyecto")):
            return await call_next(request)

        # No hay proyecto valido seleccionado.
        accept = request.headers.get("accept", "")
        if path.startswith("/api/") or "application/json" in accept:
            return JSONResponse({"detail": "No hay proyecto seleccionado"}, status_code=400)
        if request.method != "GET":
            return JSONResponse({"detail": "No hay proyecto seleccionado"}, status_code=400)
        return RedirectResponse(url="/proyectos", status_code=302)
