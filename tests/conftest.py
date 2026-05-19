import os

# Bypassea AuthMiddleware en todos los tests. Se setea antes de cualquier
# import que pueda construir la app.
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("SESSION_SECRET", "test-secret-not-for-prod")
os.environ.setdefault("SESSION_INSECURE", "1")  # cookies sin Secure en testclient (HTTP)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db_session(tmp_path):
    """Fixture que da una sesion de BD aislada por test."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    from app.modelos import Base
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
