"""Tests de CostoAdicional + integracion con endpoint cotizar."""

import pytest
from fastapi.testclient import TestClient

from app.main import crear_app
from app.modelos import Proveedor, Producto, CostoAdicional


@pytest.fixture
def cliente(db_session):
    from app.routes import get_db
    app = crear_app()
    app.dependency_overrides[get_db] = lambda: (yield db_session)
    return TestClient(app)


@pytest.fixture
def producto(db_session):
    prov = Proveedor(nombre="V1", archivo_pdf="v1.pdf")
    db_session.add(prov)
    db_session.commit()
    p = Producto(
        proveedor_id=prov.id, sku="TST-001", descripcion="Producto test",
        fob_usd=10.0, cbm=0.05, pzas_40hq=1000, categoria="casa-jaula",
    )
    db_session.add(p)
    db_session.commit()
    return p


def test_listar_sin_costos(cliente, producto):
    r = cliente.get(f"/api/productos/{producto.id}/costos-adicionales")
    assert r.status_code == 200
    j = r.json()
    assert j["items"] == []
    assert j["total_usd"] == 0
    assert j["fob_original"] == 10.0
    assert j["fob_ajustado"] == 10.0


def test_agregar_costo(cliente, producto):
    r = cliente.post(f"/api/productos/{producto.id}/costos-adicionales", json={
        "concepto": "Caja color",
        "monto_usd": 0.5,
    })
    assert r.status_code == 200
    c = r.json()
    assert c["concepto"] == "Caja color"
    assert c["monto_usd"] == 0.5
    assert c["producto_id"] == producto.id

    # Verifica que se sumo
    j = cliente.get(f"/api/productos/{producto.id}/costos-adicionales").json()
    assert j["total_usd"] == 0.5
    assert j["fob_ajustado"] == 10.5


def test_dos_costos_se_suman(cliente, producto):
    cliente.post(f"/api/productos/{producto.id}/costos-adicionales", json={
        "concepto": "Caja color", "monto_usd": 0.5,
    })
    cliente.post(f"/api/productos/{producto.id}/costos-adicionales", json={
        "concepto": "EXW->FOB", "monto_usd": 1.25,
    })
    j = cliente.get(f"/api/productos/{producto.id}/costos-adicionales").json()
    assert len(j["items"]) == 2
    assert j["total_usd"] == 1.75
    assert j["fob_ajustado"] == 11.75


def test_borrar_costo(cliente, producto):
    r = cliente.post(f"/api/productos/{producto.id}/costos-adicionales", json={
        "concepto": "Test", "monto_usd": 1.0,
    })
    cid = r.json()["id"]
    r = cliente.delete(f"/api/productos/{producto.id}/costos-adicionales/{cid}")
    assert r.status_code == 200
    j = cliente.get(f"/api/productos/{producto.id}/costos-adicionales").json()
    assert j["items"] == []


def test_costo_invalido_400(cliente, producto):
    r = cliente.post(f"/api/productos/{producto.id}/costos-adicionales", json={
        "concepto": "", "monto_usd": 1.0,
    })
    assert r.status_code == 400
    r = cliente.post(f"/api/productos/{producto.id}/costos-adicionales", json={
        "concepto": "Test", "monto_usd": 0,
    })
    assert r.status_code == 400


def test_cotizar_incluye_costos_adicionales(cliente, producto):
    """El motor debe usar fob_efectivo (fob + costos) para el calculo."""
    # Cotizacion sin costos
    r0 = cliente.get(f"/api/productos/{producto.id}/cotizar?margen_nuestro=25&margen_cliente=40")
    paso1_sin = float(r0.json()["pasos"][0]["valor"])
    assert paso1_sin == 10.0  # FOB original

    # Agregar costo
    cliente.post(f"/api/productos/{producto.id}/costos-adicionales", json={
        "concepto": "Caja color", "monto_usd": 0.5,
    })

    # Cotizacion despues
    r1 = cliente.get(f"/api/productos/{producto.id}/cotizar?margen_nuestro=25&margen_cliente=40")
    j = r1.json()
    paso1_con = float(j["pasos"][0]["valor"])
    assert paso1_con == 10.5
    assert j["fob_efectivo_usd"] == 10.5
    assert j["costos_adicionales_total_usd"] == 0.5
    assert len(j["costos_adicionales"]) == 1


def test_costo_404_producto_inexistente(cliente):
    r = cliente.post("/api/productos/99999/costos-adicionales", json={
        "concepto": "Test", "monto_usd": 1.0,
    })
    assert r.status_code == 404
