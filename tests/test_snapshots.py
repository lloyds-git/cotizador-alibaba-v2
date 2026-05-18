"""Tests de CotizacionSnapshot."""

import pytest
from fastapi.testclient import TestClient

from app.main import crear_app
from app.modelos import Proveedor, Producto, CotizacionSnapshot, CostoAdicional


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
        proveedor_id=prov.id, sku="TST-001", descripcion="Test",
        fob_usd=10.0, cbm=0.05, pzas_40hq=1000, categoria="casa-jaula",
    )
    db_session.add(p)
    db_session.commit()
    return p


def test_listar_sin_snapshots(cliente, producto):
    r = cliente.get(f"/api/productos/{producto.id}/snapshots")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_crear_snapshot_manual(cliente, producto):
    r = cliente.post(f"/api/productos/{producto.id}/snapshots", json={
        "origen": "manual",
        "tc": 20,
        "flete_maritimo_usd": 5000,
        "flete_local_mxn": 70000,
        "margen_nuestro_pct": 25,
        "margen_cliente_pct": 40,
        "descuentos_pct": 10,
        "descuentos_na_pct": 0,
        "gasto_fijo_pct": 24,
        "gastos_aduanales_pct": 5,
    })
    assert r.status_code == 200
    s = r.json()
    assert s["origen"] == "manual"
    assert s["fob_usd_efectivo"] == 10.0
    assert s["tc"] == 20
    assert s["margen_nuestro_pct"] == 25
    assert s["descuentos_pct"] == 10
    # Con la formula corregida: margen real debe ser ~25%
    assert abs(s["margen_real_pct"] - 25.0) < 0.5


def test_snapshot_con_retail_editado(cliente, producto):
    """Si paso retail_final_mxn, el margen real se calcula inverso."""
    r = cliente.post(f"/api/productos/{producto.id}/snapshots", json={
        "tc": 20,
        "margen_nuestro_pct": 25,
        "margen_cliente_pct": 40,
        "descuentos_pct": 10,
        "descuentos_na_pct": 0,
        "gasto_fijo_pct": 24,
        "retail_final_mxn": 200,  # retail bajo a proposito
    })
    s = r.json()
    assert s["retail_final_mxn"] == 200
    # Con retail tan bajo, margen real debe ser muy negativo
    assert s["margen_real_pct"] < 0


def test_snapshot_incluye_costos_adicionales(cliente, producto, db_session):
    db_session.add(CostoAdicional(producto_id=producto.id, concepto="caja", monto_usd=0.5))
    db_session.commit()
    r = cliente.post(f"/api/productos/{producto.id}/snapshots", json={
        "tc": 20, "margen_nuestro_pct": 25, "margen_cliente_pct": 40,
    })
    s = r.json()
    assert s["fob_usd_efectivo"] == 10.5
    assert s["costos_adicionales_usd"] == 0.5


def test_borrar_snapshot(cliente, producto, db_session):
    r = cliente.post(f"/api/productos/{producto.id}/snapshots", json={
        "tc": 20, "margen_nuestro_pct": 25, "margen_cliente_pct": 40,
    })
    sid = r.json()["id"]
    r = cliente.delete(f"/api/productos/{producto.id}/snapshots/{sid}")
    assert r.status_code == 200
    assert db_session.query(CotizacionSnapshot).count() == 0


def test_snapshot_producto_inexistente_404(cliente):
    r = cliente.post("/api/productos/99999/snapshots", json={"tc": 20})
    assert r.status_code == 404


def test_listar_snapshots_orden_desc(cliente, producto):
    """El historial se devuelve en orden descendente por fecha."""
    import time
    cliente.post(f"/api/productos/{producto.id}/snapshots", json={
        "tc": 20, "margen_nuestro_pct": 25, "margen_cliente_pct": 40, "notas": "primero",
    })
    time.sleep(0.01)
    cliente.post(f"/api/productos/{producto.id}/snapshots", json={
        "tc": 20, "margen_nuestro_pct": 25, "margen_cliente_pct": 40, "notas": "segundo",
    })
    items = cliente.get(f"/api/productos/{producto.id}/snapshots").json()["items"]
    assert len(items) == 2
    assert items[0]["notas"] == "segundo"  # mas reciente primero
    assert items[1]["notas"] == "primero"


def test_snapshot_recien_creado_no_es_stale(cliente, producto):
    """Un snapshot recien creado nunca es stale."""
    s = cliente.post(f"/api/productos/{producto.id}/snapshots", json={
        "tc": 20, "margen_nuestro_pct": 25, "margen_cliente_pct": 40,
    }).json()
    assert s["es_stale"] is False


def test_snapshot_es_stale_tras_editar_producto(cliente, producto, db_session):
    """Si producto.actualizado_en avanza despues del snapshot, queda stale."""
    import time
    cliente.post(f"/api/productos/{producto.id}/snapshots", json={
        "tc": 20, "margen_nuestro_pct": 25, "margen_cliente_pct": 40,
    })
    time.sleep(0.05)
    r = cliente.patch(f"/api/productos/{producto.id}", json={"cbm": 0.08})
    assert r.status_code == 200
    items = cliente.get(f"/api/productos/{producto.id}/snapshots").json()["items"]
    assert len(items) == 1
    assert items[0]["es_stale"] is True


def test_snapshot_no_es_stale_si_producto_no_cambio(cliente, producto):
    """Si el producto no se toco, el snapshot sigue fresco al listarlo."""
    cliente.post(f"/api/productos/{producto.id}/snapshots", json={
        "tc": 20, "margen_nuestro_pct": 25, "margen_cliente_pct": 40,
    })
    items = cliente.get(f"/api/productos/{producto.id}/snapshots").json()["items"]
    assert items[0]["es_stale"] is False
