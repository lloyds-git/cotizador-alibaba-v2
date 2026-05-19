"""Autenticacion via Google OAuth2 + middleware de enforcement.

Flujo:
  GET /login            -> pagina con boton "Continuar con Google"
  GET /auth/google      -> redirect a Google (Authlib)
  GET /auth/callback    -> valida token, chequea whitelist, setea sesion
  GET /logout           -> limpia sesion

Whitelist: el callback solo deja entrar si el correo del token esta en
`usuarios_autorizados` con activo=True. Sin roles: cualquier usuario
activo puede gestionar la whitelist desde /usuarios.

Tests: el middleware se bypassea con la env var AUTH_DISABLED=1
(seteada en tests/conftest.py).
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app import db as db_module
from app.modelos import UsuarioAutorizado

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Rutas que NO requieren autenticacion (path exacto o prefijo).
# Cualquier ruta no listada queda protegida.
PUBLIC_PREFIXES = (
    "/login",
    "/logout",
    "/auth/",
    "/static/",
    "/fotos/",
    "/healthz",
    "/favicon.ico",
)

oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def _es_publica(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PUBLIC_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    """Bloquea rutas protegidas si no hay session['user'].

    HTML (GET sin Accept JSON) -> redirect 302 a /login?next=...
    API o cliente JSON         -> 401 JSON
    """

    async def dispatch(self, request: Request, call_next):
        if os.environ.get("AUTH_DISABLED") == "1":
            return await call_next(request)

        path = request.url.path
        if _es_publica(path):
            return await call_next(request)

        if request.session.get("user"):
            return await call_next(request)

        accept = request.headers.get("accept", "")
        if path.startswith("/api/") or "application/json" in accept:
            return JSONResponse({"detail": "No autenticado"}, status_code=401)
        # Solo redirect en GET; otros metodos sin sesion -> 401
        if request.method != "GET":
            return JSONResponse({"detail": "No autenticado"}, status_code=401)
        return RedirectResponse(url=f"/login?next={path}", status_code=302)


@router.get("/login", response_class=HTMLResponse)
def login_pagina(request: Request, error: str | None = None, next: str = "/"):
    email = request.query_params.get("email", "")
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"error": error, "next": next, "email_rechazado": email},
    )


@router.get("/auth/google", name="auth_google")
async def auth_google(request: Request, next: str = "/"):
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI")
    if not redirect_uri:
        # Fallback: deriva del request (respeta X-Forwarded-Proto si uvicorn
        # corre con --proxy-headers).
        redirect_uri = str(request.url_for("auth_callback"))
    request.session["post_login_redirect"] = next or "/"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as e:
        return RedirectResponse(url=f"/login?error={e.error}", status_code=302)

    userinfo = token.get("userinfo") or {}
    email = (userinfo.get("email") or "").lower().strip()
    if not email:
        return RedirectResponse(url="/login?error=sin_email", status_code=302)

    SessionFactory = db_module.get_session_factory()
    with SessionFactory() as ses:
        user = (
            ses.query(UsuarioAutorizado)
            .filter(UsuarioAutorizado.email == email)
            .first()
        )
        if user is None or not user.activo:
            return RedirectResponse(
                url=f"/login?error=no_autorizado&email={email}",
                status_code=302,
            )
        user.ultimo_login = datetime.utcnow()
        if userinfo.get("name") and not user.nombre:
            user.nombre = userinfo["name"]
        ses.commit()
        usuario_session = {
            "id": user.id,
            "email": user.email,
            "nombre": user.nombre or userinfo.get("name") or user.email,
        }

    request.session["user"] = usuario_session
    next_url = request.session.pop("post_login_redirect", "/") or "/"
    # Evita open redirect: solo paths relativos
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    return RedirectResponse(url=next_url, status_code=302)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
