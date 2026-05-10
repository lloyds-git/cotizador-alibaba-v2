import pytest
from fastapi.testclient import TestClient

from app.main import crear_app
from app.modelos import Proveedor, Producto


@pytest.fixture
def cliente(db_session):
    """TestClient con BD aislada via dependency_overrides."""
    from app.routes import get_db
    app = crear_app()

    def override_get_db():
        # No cerrar la sesion del fixture; pytest la maneja
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


@pytest.fixture
def productos_demo(db_session):
    p = Proveedor(nombre="V1", archivo_pdf="v1.pdf")
    db_session.add(p)
    db_session.commit()
    for sku, fob in [("A1", 10.0), ("A2", 20.0), ("B1", 5.0)]:
        db_session.add(Producto(
            proveedor_id=p.id, sku=sku, descripcion=f"prod {sku}",
            fob_usd=fob,
        ))
    db_session.commit()
    return p


def test_listar_productos(cliente, productos_demo):
    r = cliente.get("/api/productos")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3


def test_marcar_producto(cliente, productos_demo, db_session):
    prod = db_session.query(Producto).first()
    r = cliente.post(f"/api/productos/{prod.id}/marcar", json={"marcado": True})
    assert r.status_code == 200
    db_session.refresh(prod)
    assert prod.marcado_cotizar is True


def test_actualizar_fob(cliente, productos_demo, db_session):
    prod = db_session.query(Producto).first()
    r = cliente.patch(
        f"/api/productos/{prod.id}",
        json={"fob_usd": 99.5, "notas": "Precio actualizado"},
    )
    assert r.status_code == 200
    db_session.refresh(prod)
    assert prod.fob_usd == 99.5
    assert prod.notas == "Precio actualizado"


def test_listar_marcados(cliente, productos_demo, db_session):
    productos = db_session.query(Producto).all()
    productos[0].marcado_cotizar = True
    productos[1].marcado_cotizar = True
    db_session.commit()

    r = cliente.get("/api/productos?marcados=true")
    assert r.status_code == 200
    assert r.json()["total"] == 2
