from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.modelos import Base

DB_PATH = Path(__file__).parent.parent / "data" / "productos.db"


def get_engine():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{DB_PATH}", echo=False)


def get_session_factory():
    return sessionmaker(bind=get_engine())


def init_db():
    """Crea las tablas si no existen."""
    Base.metadata.create_all(get_engine())
