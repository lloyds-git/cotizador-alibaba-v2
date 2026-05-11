"""Tests del resolver de aranceles (DB override + default-metal + estatico)."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.cotizador.lookup import resolver_arancel
from app.modelos import ArancelOverride


@pytest.fixture
def cliente(db_session):
    from app.main import crear_app
    from app.routes import get_db
    app = crear_app()
    app.dependency_overrides[get_db] = lambda: (yield db_session)
    return TestClient(app)


def test_default_25_plastico(db_session):
    """Plastico sin categoria conocida -> 25%."""
    r = resolver_arancel(db_session, categoria=None, subcategoria=None, material="PP")
    assert r.fuente == "default-25"
    assert r.tasa_pct == Decimal("25")


def test_default_metal_35(db_session):
    """Material con 'steel' -> 35% sin pasar por estatico."""
    r = resolver_arancel(db_session, categoria="alimentadores", subcategoria=None,
                         material="stainless steel bowls")
    assert r.fuente == "default-metal"
    assert r.tasa_pct == Decimal("35")


def test_default_metal_acero(db_session):
    """Acero inoxidable en bebederos -> 35% (gana sobre estatico)."""
    r = resolver_arancel(db_session, categoria="bebederos", subcategoria=None,
                         material="Acero inoxidable 304")
    assert r.fuente == "default-metal"
    assert r.tasa_pct == Decimal("35")


def test_estatico_casa_jaula_plastico(db_session):
    """Casa-jaula plastica cae al estatico (Mascotas/Casetas = 15%)."""
    r = resolver_arancel(db_session, categoria="casa-jaula", subcategoria=None,
                         material="PP")
    assert r.fuente == "tariffs-estatico"
    assert r.tasa_pct == Decimal("15")
    assert r.fraccion == "9403.89.99"


def test_override_db_gana_sobre_default_metal(db_session):
    """Override en DB tiene prioridad sobre cualquier default."""
    db_session.add(ArancelOverride(
        categoria="alimentadores", material_pattern="steel",
        fraccion="7323.99.99", tasa_pct=20.0, nota="test",
    ))
    db_session.commit()
    r = resolver_arancel(db_session, categoria="alimentadores", subcategoria=None,
                         material="stainless steel bowls")
    assert r.fuente == "override-db"
    assert r.tasa_pct == Decimal("20.0")
    assert r.fraccion == "7323.99.99"


def test_override_especifico_gana_sobre_generico(db_session):
    """Override con cat+material tiene mas prioridad que solo categoria."""
    db_session.add(ArancelOverride(
        categoria="alimentadores", material_pattern=None,
        fraccion="1111.11.11", tasa_pct=10.0,
    ))
    db_session.add(ArancelOverride(
        categoria="alimentadores", material_pattern="steel",
        fraccion="2222.22.22", tasa_pct=30.0,
    ))
    db_session.commit()
    # Producto de acero: gana el especifico
    r = resolver_arancel(db_session, categoria="alimentadores", subcategoria=None,
                         material="stainless steel")
    assert r.fraccion == "2222.22.22"
    # Producto plastico: gana el generico de categoria
    r = resolver_arancel(db_session, categoria="alimentadores", subcategoria=None,
                         material="PP")
    assert r.fraccion == "1111.11.11"


def test_endpoint_aranceles_crud(cliente, db_session):
    # Listar vacio
    assert cliente.get("/api/aranceles").json() == {"items": []}
    # Crear
    r = cliente.post("/api/aranceles", json={
        "categoria": "rejas",
        "material_pattern": "metal",
        "fraccion": "7323.99.99",
        "tasa_pct": 35.0,
        "nota": "test",
    })
    assert r.status_code == 200
    arancel_id = r.json()["id"]
    # Listar
    items = cliente.get("/api/aranceles").json()["items"]
    assert len(items) == 1
    # Editar
    r = cliente.patch(f"/api/aranceles/{arancel_id}", json={
        "categoria": "rejas",
        "material_pattern": "metal",
        "fraccion": "7323.99.99",
        "tasa_pct": 40.0,
    })
    assert r.json()["tasa_pct"] == 40.0
    # Borrar
    r = cliente.delete(f"/api/aranceles/{arancel_id}")
    assert r.status_code == 200
    assert cliente.get("/api/aranceles").json() == {"items": []}
