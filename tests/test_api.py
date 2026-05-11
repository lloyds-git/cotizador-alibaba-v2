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
    for sku, fob, cat in [
        ("A1", 10.0, "casa-jaula"),
        ("A2", 20.0, "casa-jaula"),
        ("B1", 5.0, "alimentadores"),
    ]:
        db_session.add(Producto(
            proveedor_id=p.id, sku=sku, descripcion=f"prod {sku}",
            fob_usd=fob, categoria=cat,
        ))
    # Uno sin categoria para probar el filtro sin_categoria
    db_session.add(Producto(
        proveedor_id=p.id, sku="X", descripcion="sin categoria",
        fob_usd=1.0,
    ))
    db_session.commit()
    return p


def test_listar_productos(cliente, productos_demo):
    r = cliente.get("/api/productos")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 4
    assert len(data["items"]) == 4
    # Verifica que la respuesta incluye categoria
    keys = data["items"][0].keys()
    assert "categoria" in keys
    assert "subcategoria" in keys


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


def test_actualizar_categoria(cliente, productos_demo, db_session):
    prod = db_session.query(Producto).filter(Producto.categoria.is_(None)).first()
    assert prod is not None
    r = cliente.patch(
        f"/api/productos/{prod.id}",
        json={"categoria": "rejas"},
    )
    assert r.status_code == 200
    db_session.refresh(prod)
    assert prod.categoria == "rejas"


def test_actualizar_categoria_vacia_limpia(cliente, productos_demo, db_session):
    prod = db_session.query(Producto).filter(Producto.categoria == "casa-jaula").first()
    r = cliente.patch(f"/api/productos/{prod.id}", json={"categoria": ""})
    assert r.status_code == 200
    db_session.refresh(prod)
    assert prod.categoria is None


def test_listar_marcados(cliente, productos_demo, db_session):
    productos = db_session.query(Producto).all()
    productos[0].marcado_cotizar = True
    productos[1].marcado_cotizar = True
    db_session.commit()

    r = cliente.get("/api/productos?marcados=true")
    assert r.status_code == 200
    assert r.json()["total"] == 2


def test_filtro_por_categoria(cliente, productos_demo):
    r = cliente.get("/api/productos?categoria=casa-jaula")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert all(it["categoria"] == "casa-jaula" for it in items)


def test_filtro_sin_categoria(cliente, productos_demo):
    r = cliente.get("/api/productos?categoria=__sin_categoria__")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["categoria"] is None


def test_listar_categorias(cliente, productos_demo):
    r = cliente.get("/api/categorias")
    assert r.status_code == 200
    items = r.json()["items"]
    # Esperamos: casa-jaula (2), alimentadores (1), None (1)
    cats = {it["categoria"]: it["total"] for it in items}
    assert cats["casa-jaula"] == 2
    assert cats["alimentadores"] == 1
    assert cats[None] == 1


def test_marcar_bulk(cliente, productos_demo, db_session):
    prods = db_session.query(Producto).filter(Producto.categoria == "casa-jaula").all()
    ids = [p.id for p in prods]
    r = cliente.post("/api/productos/marcar-bulk", json={"ids": ids, "marcado": True})
    assert r.status_code == 200
    assert r.json()["afectados"] == 2
    for p in prods:
        db_session.refresh(p)
        assert p.marcado_cotizar is True


def test_marcar_bulk_desmarcar(cliente, productos_demo, db_session):
    # Pre-marcar
    db_session.query(Producto).update({Producto.marcado_cotizar: True})
    db_session.commit()
    ids = [p.id for p in db_session.query(Producto).all()]
    r = cliente.post("/api/productos/marcar-bulk", json={"ids": ids, "marcado": False})
    assert r.status_code == 200
    for p in db_session.query(Producto).all():
        assert p.marcado_cotizar is False


def test_marcar_bulk_vacio(cliente, productos_demo):
    r = cliente.post("/api/productos/marcar-bulk", json={"ids": [], "marcado": True})
    assert r.status_code == 200
    assert r.json()["afectados"] == 0


def test_exportar_categoria_inexistente_404(cliente, productos_demo):
    r = cliente.get("/exportar/no-existe-cat")
    assert r.status_code == 404
