"""Validaciones de renombrar/fusionar proveedor: no perder archivo_pdf."""
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


def _prov(db, nombre, pdf=None):
    p = Proveedor(nombre=nombre, archivo_pdf=pdf)
    db.add(p)
    db.commit()
    return p


def test_rename_simple_preserva_archivo_pdf(cliente, db_session):
    """Renombrar (sin match a otro proveedor) NO debe tocar archivo_pdf."""
    pv = _prov(db_session, "Tianjin Goodrich-1", pdf="Tianjin Goodrich-1.pdf")
    r = cliente.patch(f"/api/proveedores/{pv.id}", json={"nombre": "Tianjin Goodrich MX"})
    assert r.status_code == 200
    assert r.json()["fusionado"] is False
    db_session.refresh(pv)
    assert pv.nombre == "Tianjin Goodrich MX"
    assert pv.archivo_pdf == "Tianjin Goodrich-1.pdf"  # referencia intacta


def test_merge_hereda_archivo_pdf_si_destino_no_tiene(cliente, db_session):
    """Al fusionar en un destino sin archivo_pdf, hereda el del origen."""
    destino = _prov(db_session, "Flores ABC", pdf=None)
    origen = _prov(db_session, "Origen Temporal", pdf="cotizacion-origen.pdf")
    db_session.add(Producto(proveedor_id=origen.id, sku="X1", descripcion="rosa artificial", fob_usd=1.0))
    db_session.commit()

    # Renombrar origen al nombre del destino -> dispara fusion.
    r = cliente.patch(f"/api/proveedores/{origen.id}", json={"nombre": "Flores ABC"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fusionado"] is True
    assert body["proveedor_id"] == destino.id

    db_session.refresh(destino)
    # El destino heredo el PDF del origen (no se perdio la referencia).
    assert destino.archivo_pdf == "cotizacion-origen.pdf"
    # El producto movido resuelve su PDF via el destino.
    prod = db_session.query(Producto).filter_by(sku="X1").first()
    assert prod.proveedor_id == destino.id


def test_origen_prefiere_pdf_del_producto(cliente, db_session):
    """El 'Cotizacion original' usa el archivo_pdf del PRODUCTO sobre el del proveedor."""
    pv = _prov(db_session, "Prov", pdf="del-proveedor.pdf")
    prod = Producto(proveedor_id=pv.id, sku="Z", descripcion="rosa", fob_usd=1.0,
                    archivo_pdf="del-producto.pdf")
    db_session.add(prod)
    db_session.commit()
    r = cliente.get(f"/api/productos/{prod.id}/origen")
    assert r.status_code == 200
    j = r.json()
    # Ningun archivo existe en disco -> existe False, pero referencia el del PRODUCTO.
    assert j["existe"] is False
    assert j["archivo_pdf"] == "del-producto.pdf"


def test_merge_conserva_archivo_pdf_del_destino_si_ya_tiene(cliente, db_session):
    """Si el destino ya tiene archivo_pdf, se conserva el suyo (no lo pisa)."""
    destino = _prov(db_session, "Flores ABC", pdf="destino.pdf")
    origen = _prov(db_session, "Origen Temporal", pdf="origen.pdf")
    db_session.add(Producto(proveedor_id=origen.id, sku="Y1", descripcion="peonia artificial", fob_usd=1.0))
    db_session.commit()

    r = cliente.patch(f"/api/proveedores/{origen.id}", json={"nombre": "Flores ABC"})
    assert r.status_code == 200
    assert r.json()["fusionado"] is True
    db_session.refresh(destino)
    assert destino.archivo_pdf == "destino.pdf"  # conserva el suyo
