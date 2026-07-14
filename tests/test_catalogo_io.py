"""Round-trip de export/import del catalogo (app/catalogo_io.py)."""
import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import catalogo_io
from app.main import crear_app
from app.modelos import (
    Arancel,
    ArancelOverride,
    Base,
    Categoria,
    CategoriaKeyword,
    PatronDescarte,
)


def _nueva_sesion(tmp_path, nombre):
    engine = create_engine(f"sqlite:///{tmp_path / nombre}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _sembrar_dev(s):
    cat = Categoria(
        slug="pajaros", orden=10, fraccion="8306.29.99", tasa_pct=15.0,
        arancel_estado="confirmado", arancel_nota="investigado IA",
        competencia_sitios="amazon.com.mx,mercadolibre.com.mx",
    )
    s.add(cat)
    s.flush()
    s.add_all([
        CategoriaKeyword(categoria_id=cat.id, keyword="hummingbird feeder"),
        CategoriaKeyword(categoria_id=cat.id, keyword="bird cage"),
    ])
    s.add(Arancel(categoria="Mascotas", subcategoria="Jaulas",
                  fraccion="7323.99.99", tasa_pct=20.0, nota="acero"))
    s.add(ArancelOverride(categoria="pajaros", material_pattern="steel",
                          fraccion="7323.99.01", tasa_pct=25.0))
    s.add(ArancelOverride(categoria=None, material_pattern=None,
                          fraccion="9999.99.99", tasa_pct=25.0, nota="fallback"))
    s.add(PatronDescarte(patron=r"^precio\s", nota="nota de pricing"))
    s.commit()


def test_roundtrip_export_import(tmp_path):
    dev = _nueva_sesion(tmp_path, "dev.db")
    _sembrar_dev(dev)
    data = catalogo_io.exportar_catalogo(dev, proyecto="principal")

    assert data["formato"] == catalogo_io.FORMATO
    assert data["proyecto"] == "principal"
    assert len(data["categorias"]) == 1
    assert len(data["aranceles_override"]) == 2

    prod = _nueva_sesion(tmp_path, "prod.db")  # BD vacia
    res = catalogo_io.importar_catalogo(prod, data)
    assert res["categorias"]["creadas"] == 1
    assert res["aranceles"]["creadas"] == 1
    assert res["aranceles_override"]["creadas"] == 2
    assert res["patrones_descarte"]["creadas"] == 1

    cat = prod.query(Categoria).filter_by(slug="pajaros").one()
    assert cat.fraccion == "8306.29.99"
    assert cat.tasa_pct == 15.0
    assert cat.arancel_estado == "confirmado"
    kws = {k.keyword for k in prod.query(CategoriaKeyword).all()}
    assert kws == {"hummingbird feeder", "bird cage"}
    ov = prod.query(ArancelOverride).filter_by(categoria=None, material_pattern=None).one()
    assert ov.fraccion == "9999.99.99"


def test_import_es_upsert_no_duplica(tmp_path):
    dev = _nueva_sesion(tmp_path, "dev.db")
    _sembrar_dev(dev)
    data = catalogo_io.exportar_catalogo(dev)

    prod = _nueva_sesion(tmp_path, "prod.db")
    catalogo_io.importar_catalogo(prod, data)
    # Segunda importacion: debe actualizar, no crear duplicados.
    res2 = catalogo_io.importar_catalogo(prod, data)
    assert res2["categorias"]["creadas"] == 0
    assert res2["categorias"]["actualizadas"] == 1
    assert res2["aranceles_override"]["creadas"] == 0
    assert prod.query(Categoria).count() == 1
    assert prod.query(ArancelOverride).count() == 2
    assert prod.query(CategoriaKeyword).count() == 2  # no se duplicaron


def test_import_preserva_datos_existentes(tmp_path):
    dev = _nueva_sesion(tmp_path, "dev.db")
    _sembrar_dev(dev)
    data = catalogo_io.exportar_catalogo(dev)

    prod = _nueva_sesion(tmp_path, "prod.db")
    prod.add(Categoria(slug="solo-en-prod", orden=99))  # dato que solo vive en prod
    prod.commit()
    catalogo_io.importar_catalogo(prod, data)
    assert prod.query(Categoria).filter_by(slug="solo-en-prod").count() == 1


def test_import_rechaza_formato_invalido(tmp_path):
    prod = _nueva_sesion(tmp_path, "prod.db")
    with pytest.raises(catalogo_io.CatalogoInvalido):
        catalogo_io.importar_catalogo(prod, {"formato": "otra-cosa"})


# --------------------------- endpoints web ---------------------------------

def _cliente(sesion):
    from app.routes import get_db
    app = crear_app()
    app.dependency_overrides[get_db] = lambda: (yield sesion)
    c = TestClient(app)
    c.cookies.set("session", "x")  # placeholder; auth deshabilitado en tests
    return c


def test_endpoint_export_import_roundtrip(tmp_path):
    dev = _nueva_sesion(tmp_path, "dev.db")
    _sembrar_dev(dev)

    # Export: descarga JSON con Content-Disposition attachment.
    cli_dev = _cliente(dev)
    r = cli_dev.get("/api/catalogo/exportar")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    payload = r.json()
    assert payload["formato"] == catalogo_io.FORMATO

    # Import en BD vacia via multipart.
    prod = _nueva_sesion(tmp_path, "prod.db")
    cli_prod = _cliente(prod)
    archivo = io.BytesIO(json.dumps(payload).encode("utf-8"))
    r2 = cli_prod.post(
        "/api/catalogo/importar",
        files={"archivo": ("catalogo.json", archivo, "application/json")},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["categorias"]["creadas"] == 1
    assert prod.query(Categoria).filter_by(slug="pajaros").count() == 1


def test_endpoint_import_json_invalido(tmp_path):
    prod = _nueva_sesion(tmp_path, "prod.db")
    cli = _cliente(prod)
    archivo = io.BytesIO(b"{no es json valido")
    r = cli.post(
        "/api/catalogo/importar",
        files={"archivo": ("x.json", archivo, "application/json")},
    )
    assert r.status_code == 422
