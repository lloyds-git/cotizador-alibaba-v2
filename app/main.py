from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import init_db, get_session_factory
from app.routes import router


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


def crear_app() -> FastAPI:
    init_db()
    _auto_seed_aranceles()
    app = FastAPI(title="Mascotas BD")
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
