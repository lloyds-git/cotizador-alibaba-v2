from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.routes import router


def crear_app() -> FastAPI:
    init_db()
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
    uvicorn.run(app, host="127.0.0.1", port=8080)
