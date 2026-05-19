import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# Carga .env antes de leer cualquier env var (GOOGLE_CLIENT_ID etc.)
load_dotenv()

from app.db import init_db, get_session_factory
from app.routes import router
from app.auth import router as auth_router, AuthMiddleware


def _auto_seed_aranceles() -> None:
    """Si la tabla `aranceles` esta vacia al arrancar, la siembra desde
    config/aranceles.yml. Bootstrap idempotente: si ya tiene filas no hace
    nada (preserva ediciones manuales hechas via UI)."""
    try:
        from app.modelos import Arancel
        from scripts.seed_aranceles import seed
        Session = get_session_factory()
        with Session() as ses:
            if ses.query(Arancel).count() > 0:
                return
        # Tabla vacia -> sembrar
        seed(reset=False)
    except Exception as e:
        # Bootstrap no debe tumbar el arranque si algo falla (ej. YAML
        # malformado o tabla aun no migrada). Solo log.
        import logging
        logging.getLogger(__name__).warning("auto-seed aranceles fallo: %s", e)


def _auto_seed_admin_inicial() -> None:
    """Si la tabla `usuarios_autorizados` esta vacia y .env tiene
    ADMIN_INICIAL, inserta ese correo. Sin esto, nadie puede entrar a la
    app despues de activar auth (chicken-and-egg)."""
    email = (os.environ.get("ADMIN_INICIAL") or "").strip().lower()
    if not email or "@" not in email:
        return
    try:
        from app.modelos import UsuarioAutorizado
        Session = get_session_factory()
        with Session() as ses:
            if ses.query(UsuarioAutorizado).count() > 0:
                return
            ses.add(UsuarioAutorizado(email=email, activo=True))
            ses.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("auto-seed admin inicial fallo: %s", e)


def crear_app() -> FastAPI:
    init_db()
    _auto_seed_aranceles()
    _auto_seed_admin_inicial()
    app = FastAPI(title="Mascotas BD")

    # Orden de middlewares: add_middleware envuelve (LIFO). Para que
    # AuthMiddleware pueda leer request.session, SessionMiddleware tiene que
    # estar mas afuera -> se anade DESPUES.
    session_secret = os.environ.get("SESSION_SECRET")
    if not session_secret:
        # Sin SESSION_SECRET: genera uno efimero (las sesiones se invalidan
        # al reiniciar). Esta bien en tests y dev; en prod debe estar puesto.
        session_secret = secrets.token_hex(32)
    https_only = os.environ.get("SESSION_INSECURE") != "1"

    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        https_only=https_only,
        same_site="lax",
        max_age=60 * 60 * 8,  # 8 horas
    )

    app.include_router(auth_router)
    app.include_router(router)

    # Servir fotos como archivos estaticos
    fotos_dir = Path(__file__).parent.parent / "data" / "fotos"
    fotos_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/fotos", StaticFiles(directory=str(fotos_dir)), name="fotos")

    return app


app = crear_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8081)
