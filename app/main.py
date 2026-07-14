import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

# Carga .env antes de leer cualquier env var (GOOGLE_CLIENT_ID etc.)
load_dotenv()

from app.db import init_sistema_db, asegurar_template, get_sistema_session_factory
from app.routes import router
from app.routes_proyectos import router as proyectos_router
from app.auth import router as auth_router, AuthMiddleware
from app.proyecto_middleware import ProyectoMiddleware


def _auto_seed_admin_inicial() -> None:
    """Si la tabla `usuarios_autorizados` (BD de sistema) esta vacia y .env tiene
    ADMIN_INICIAL, inserta ese correo. Sin esto, nadie puede entrar a la
    app despues de activar auth (chicken-and-egg)."""
    email = (os.environ.get("ADMIN_INICIAL") or "").strip().lower()
    if not email or "@" not in email:
        return
    try:
        from app.modelos import UsuarioAutorizado
        Session = get_sistema_session_factory()
        with Session() as ses:
            if ses.query(UsuarioAutorizado).count() > 0:
                return
            ses.add(UsuarioAutorizado(email=email, activo=True))
            ses.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("auto-seed admin inicial fallo: %s", e)


def crear_app() -> FastAPI:
    # Arranque multi-proyecto: crea la BD de sistema (auth + registro de
    # proyectos) y garantiza la plantilla base. Las BDs de proyecto se crean
    # bajo demanda al crear un proyecto (clonando la plantilla).
    init_sistema_db()
    asegurar_template()
    _auto_seed_admin_inicial()
    app = FastAPI(title="Mascotas BD")

    # Orden de middlewares: add_middleware envuelve (LIFO); el ultimo anadido
    # queda mas afuera. Ejecucion deseada: Session -> Auth -> Proyecto -> app,
    # asi que se anaden en orden inverso (Proyecto primero, Session al final).
    session_secret = os.environ.get("SESSION_SECRET")
    if not session_secret:
        # Sin SESSION_SECRET: genera uno efimero (las sesiones se invalidan
        # al reiniciar). Esta bien en tests y dev; en prod debe estar puesto.
        session_secret = secrets.token_hex(32)
    https_only = os.environ.get("SESSION_INSECURE") != "1"

    app.add_middleware(ProyectoMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        https_only=https_only,
        same_site="lax",
        max_age=60 * 60 * 8,  # 8 horas
    )

    @app.get("/healthz")
    def healthz():
        # Endpoint publico (ver PUBLIC_PREFIXES) para health check y para
        # verificar que build/codigo corre en cada instancia sin pasar por login.
        return {"ok": True, "app": "cotizav2"}

    app.include_router(auth_router)
    app.include_router(proyectos_router)
    app.include_router(router)

    # Las fotos se sirven por proyecto via GET /fotos/{ruta} (en routes.py),
    # que resuelve la carpeta del proyecto activo desde la sesion.

    return app


app = crear_app()


if __name__ == "__main__":
    import uvicorn
    # Puerto/host desde .env (APP_PORT, default 8071). run-local.bat usa esto:
    # 'python -m app.main'. --reload activo salvo APP_RELOAD=0.
    port = int(os.environ.get("APP_PORT", "8071"))
    host = os.environ.get("APP_HOST", "0.0.0.0")
    reload = os.environ.get("APP_RELOAD", "1") != "0"
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)
