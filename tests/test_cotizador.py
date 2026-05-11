"""Tests del motor 14 pasos + endpoint /api/productos/<id>/cotizar."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import crear_app
from app.modelos import Proveedor, Producto


@pytest.fixture
def cliente(db_session):
    from app.routes import get_db
    app = crear_app()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


@pytest.fixture
def producto_completo(db_session):
    prov = Proveedor(nombre="V1", archivo_pdf="v1.pdf")
    db_session.add(prov)
    db_session.commit()
    p = Producto(
        proveedor_id=prov.id,
        sku="TEST-001",
        descripcion="Plastic pet kennel",
        fob_usd=10.0,
        cbm=0.05,
        pzas_40hq=1000,
        categoria="casa-jaula",
    )
    db_session.add(p)
    db_session.commit()
    return p


def test_motor_14_pasos_basico():
    """compute_for_row con un row valido devuelve 14 pasos."""
    from app.cotizador.engine import compute_for_row

    row = {
        "unit_price": 10.0,
        "piezas_contenedor": 1000,
        "category": "Mascotas",
        "subcategory": "Casetas",
    }
    r = compute_for_row(row)
    for i in range(1, 15):
        assert getattr(r, f"paso{i}") is not None
    assert r.tasa_arancelaria_pct == Decimal("15")  # casetas = 15%
    assert r.fraccion_arancelaria == "9403.89.99"
    assert not r.warnings


def test_motor_warning_sin_fob():
    """Sin precio_base devuelve warnings y pasos en 0."""
    from app.cotizador.engine import compute_for_row

    r = compute_for_row({"unit_price": None, "piezas_contenedor": 100})
    assert r.warnings
    assert r.paso1 == Decimal("0")


def test_adapter_mapea_categoria_interna():
    """producto_a_row mapea casa-jaula a (Mascotas, Casetas)."""
    from app.cotizador.adapter import producto_a_row

    class P:  # mock minimo
        fob_usd = 10.0
        pzas_40hq = 100
        pzas_20ft = None
        categoria = "casa-jaula"
        subcategoria = None
        cbm = 0.05

    row = producto_a_row(P())
    assert row["category"] == "Mascotas"
    assert row["subcategory"] == "Casetas"
    assert row["unit_price"] == 10.0
    assert row["piezas_contenedor"] == 100


def test_endpoint_cotizar_404_si_no_existe(cliente, producto_completo):
    r = cliente.get("/api/productos/99999/cotizar")
    assert r.status_code == 404


def test_endpoint_cotizar_devuelve_14_pasos(cliente, producto_completo):
    r = cliente.get(
        f"/api/productos/{producto_completo.id}/cotizar"
        "?tc=20&margen_nuestro=0.15&margen_cliente=0.40"
    )
    assert r.status_code == 200
    data = r.json()
    assert data["sku"] == "TEST-001"
    assert data["categoria"] == "casa-jaula"
    assert len(data["pasos"]) == 14
    assert data["pasos"][0]["n"] == 1
    assert data["pasos"][13]["n"] == 14
    # Cada paso tiene label y valor
    for paso in data["pasos"]:
        assert "label" in paso
        assert "valor" in paso
    # Fields completos para el panel lateral
    for campo in ["material", "medidas", "moq", "packing", "carton_dims",
                  "cbm", "pzas_40hq", "lead_time", "proveedor",
                  "fraccion_arancelaria", "tasa_arancelaria_pct"]:
        assert campo in data


def test_margen_real_coincide_con_configurado():
    """Formula corregida (Salo 2026-05-11): el margen Lloyds configurado
    debe ser margen real (utilidad/venta), no margen sobre costo.

    Con landing=$100, mn=25%, td=34%, mc=40%:
      venta esperada = 100 / (1 - 0.25 - 0.34) = 100 / 0.41 = $243.90
      gastos = 243.90 * 0.34 = $82.93
      costo_total = 100 + 82.93 = $182.93
      utilidad = 243.90 - 182.93 = $60.97
      margen_real = 60.97 / 243.90 = 25.00%
    """
    from app.cotizador.engine import compute_for_row

    row = {
        "unit_price": 1.0,
        "piezas_contenedor": 1,
    }
    settings = {
        "tc_usd_mxn": 100,        # landing = 100 directo
        "flete_maritimo_usd": 0,
        "flete_local_mxn": 0,
        "dta_pct": 0,
        "gastos_aduanales_pct": 0,
        "descuentos_pct": 10,
        "descuentos_na_pct": 0,
        "gasto_fijo_pct": 24,
    }
    r = compute_for_row(
        row,
        settings=settings,
        override_tasa_pct=0,
        margen_nuestro_pct=25,
        margen_cliente_pct=40,
    )
    landing = float(r.paso9)
    venta = float(r.paso11)
    td = 0.34
    costo_total = landing + venta * td
    utilidad = venta - costo_total
    margen_real = utilidad / venta

    assert abs(margen_real - 0.25) < 0.001, (
        f"margen real {margen_real:.4f} no es 25%. "
        f"landing={landing:.2f} venta={venta:.2f} td={td}"
    )


def test_endpoint_cotizar_override_tc(cliente, producto_completo):
    """Cambiar TC mueve el paso 7 (a MXN)."""
    r1 = cliente.get(
        f"/api/productos/{producto_completo.id}/cotizar?tc=20&margen_nuestro=0.15&margen_cliente=0.40"
    )
    r2 = cliente.get(
        f"/api/productos/{producto_completo.id}/cotizar?tc=25&margen_nuestro=0.15&margen_cliente=0.40"
    )
    assert r1.status_code == 200 and r2.status_code == 200
    paso7_tc20 = Decimal(r1.json()["pasos"][6]["valor"])
    paso7_tc25 = Decimal(r2.json()["pasos"][6]["valor"])
    # con TC mas alto, paso7 (en MXN) debe ser mayor
    assert paso7_tc25 > paso7_tc20
