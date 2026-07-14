"""Router de gestion de proyectos (BD por proyecto).

Un proyecto = una BD de negocio (data/proyectos/<slug>/productos.db) clonada de
la plantilla + sus carpetas fotos/exports. El registro de proyectos vive en la
BD de sistema (data/sistema.db). El proyecto activo se guarda en
session['proyecto'].

Estas rutas estan EXENTAS de ProyectoMiddleware (para poder crear/elegir uno
sin tener aun ninguno seleccionado), pero requieren login (AuthMiddleware).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db as db_module
from app.modelos import Proyecto
from app.routes import SistemaDep

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _validar_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not db_module.slug_valido(slug):
        raise HTTPException(
            422,
            "slug invalido: usa minusculas, numeros, guion o guion-bajo "
            "(ej. 'cliente-a'). Empieza con letra/numero, sin espacios ni acentos.",
        )
    return slug


def _proyecto_a_dict(p: Proyecto) -> dict:
    return {
        "id": p.id,
        "slug": p.slug,
        "nombre": p.nombre,
        "activo": p.activo,
        "creado_en": p.creado_en.isoformat() if p.creado_en else None,
        "ultimo_uso": p.ultimo_uso.isoformat() if p.ultimo_uso else None,
    }


@router.get("/api/proyectos")
def listar_proyectos(db: SistemaDep, request: Request):
    """Lista de proyectos para el selector del nav."""
    # En SQLite, DESC ordena los NULL al final: recientes primero, nunca-usados
    # despues (evita depender de NULLS LAST explicito).
    items = (
        db.query(Proyecto)
        .filter(Proyecto.activo == True)  # noqa: E712
        .order_by(Proyecto.ultimo_uso.desc(), Proyecto.nombre.asc())
        .all()
    )
    return {
        "activo": request.session.get("proyecto"),
        "items": [_proyecto_a_dict(p) for p in items],
    }


@router.get("/proyectos", response_class=HTMLResponse)
def pagina_proyectos(request: Request, db: SistemaDep):
    """Pagina para elegir/crear proyecto."""
    items = (
        db.query(Proyecto)
        .order_by(Proyecto.activo.desc(), Proyecto.nombre.asc())
        .all()
    )
    return TEMPLATES.TemplateResponse(
        request,
        "proyectos.html",
        {
            "proyectos": [_proyecto_a_dict(p) for p in items],
            "activo": request.session.get("proyecto"),
        },
    )


@router.post("/proyectos")
def crear_proyecto_endpoint(
    request: Request,
    db: SistemaDep,
    slug: str = Form(...),
    nombre: str = Form(""),
):
    """Crea un proyecto: clona la plantilla, registra en sistema.db y lo activa."""
    slug = _validar_slug(slug)
    if db.query(Proyecto).filter_by(slug=slug).first():
        raise HTTPException(409, f"Ya existe un proyecto con slug '{slug}'")
    try:
        db_module.crear_proyecto(slug)  # clona _template.db + crea fotos/exports
    except FileExistsError:
        # BD huerfana en disco sin registro: la reusamos en vez de fallar.
        pass
    p = Proyecto(slug=slug, nombre=(nombre.strip() or slug), activo=True,
                 ultimo_uso=datetime.utcnow())
    db.add(p)
    db.commit()
    request.session["proyecto"] = slug
    return RedirectResponse(url="/", status_code=302)


@router.post("/proyectos/seleccionar")
def seleccionar_proyecto(
    request: Request,
    db: SistemaDep,
    slug: str = Form(...),
):
    """Cambia el proyecto activo en la sesion."""
    slug = _validar_slug(slug)
    p = db.query(Proyecto).filter_by(slug=slug, activo=True).first()
    if p is None:
        raise HTTPException(404, f"Proyecto '{slug}' no existe")
    if not db_module.ruta_bd_proyecto(slug).exists():
        raise HTTPException(409, f"El proyecto '{slug}' no tiene BD en disco")
    p.ultimo_uso = datetime.utcnow()
    db.commit()
    request.session["proyecto"] = slug
    # HTMX/fetch: 204 para que el front recargue; navegacion normal: redirect.
    if request.headers.get("hx-request") or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True, "proyecto": slug})
    return RedirectResponse(url="/", status_code=302)
