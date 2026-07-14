"""Tests de la busqueda de competencia con sitios por categoria + campo manual.

Cubre:
- Resolucion de dominios (CSV categoria + extra, dedup, host de URL, default).
- Normalizacion dinamica: marketplace derivado del host; sitios extra sobreviven.
- Endpoint PATCH de sitios por categoria (normaliza + persiste + serializa).
- Wiring del endpoint de busqueda: resuelve dominios desde la categoria del
  producto + sitios_extra (monkeypatch, sin llamar a la API real).
- Resumen de mercado dinamico (canonicos primero, extra, global al final).
"""
import types

import pytest
from fastapi.testclient import TestClient

from app import competencia
from app.main import crear_app
from app.modelos import Categoria, Producto, Proveedor


@pytest.fixture
def cliente(db_session):
    from app.routes import get_db
    app = crear_app()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


# --------------------------- unit: resolver_dominios ---------------------------

def test_resolver_dominios_default_sin_config():
    assert competencia.resolver_dominios(None, None) == [
        "amazon.com.mx", "mercadolibre.com.mx", "petco.com.mx",
    ]


def test_resolver_dominios_suma_categoria_y_extra_dedup_y_host():
    r = competencia.resolver_dominios(
        "amazon.com.mx, petco.com.mx",
        "https://www.tiendanimal.com.mx/perros?x=1  petco.com.mx",
    )
    assert r == ["amazon.com.mx", "petco.com.mx", "tiendanimal.com.mx"]


def test_resolver_dominios_solo_extra():
    assert competencia.resolver_dominios("", "petsy.mx") == ["petsy.mx"]


def test_host_de_url():
    assert competencia._host_de_url("HTTPS://Www.Amazon.com.mx/dp/B0/ref") == "amazon.com.mx"
    assert competencia._host_de_url("articulo.mercadolibre.com.mx/MLM-1") == "articulo.mercadolibre.com.mx"
    assert competencia._host_de_url("petco.com.mx") == "petco.com.mx"
    assert competencia._host_de_url("") == ""


def test_key_de_dominio():
    assert competencia._key_de_dominio("amazon.com.mx") == "amazon_mx"
    assert competencia._key_de_dominio("articulo.mercadolibre.com.mx") == "mercadolibre_mx"
    assert competencia._key_de_dominio("tiendanimal.com.mx") == "tiendanimal.com.mx"


# ---------------------------- unit: _normalizar --------------------------------

def test_normalizar_canonico_deriva_del_host():
    dom = ["amazon.com.mx", "petco.com.mx"]
    it = {"titulo": "Cama", "url": "https://www.amazon.com.mx/dp/B0X",
          "precio_mxn": "$1,299.00", "num_reviews": "12", "confianza_match": "0.9"}
    n = competencia._normalizar(it, dom)
    assert n["marketplace"] == "amazon_mx"
    assert n["precio_mxn"] == 1299.0
    assert n["num_reviews"] == 12


def test_normalizar_sitio_extra_sobrevive():
    dom = ["amazon.com.mx", "tiendanimal.com.mx"]
    it = {"titulo": "Cama XL", "url": "https://tiendanimal.com.mx/cama-xl", "precio": "999"}
    n = competencia._normalizar(it, dom)
    assert n["marketplace"] == "tiendanimal.com.mx"
    assert n["precio_mxn"] == 999.0


def test_conto_busquedas_cuenta_server_tool_use():
    def blk(t):
        return types.SimpleNamespace(type=t)
    resp0 = types.SimpleNamespace(content=[blk("text")])  # rechazo: no busco
    resp2 = types.SimpleNamespace(content=[
        blk("server_tool_use"), blk("web_search_tool_result"),
        blk("server_tool_use"), blk("text"),
    ])
    assert competencia._conto_busquedas(resp0) == 0
    assert competencia._conto_busquedas(resp2) == 2


def test_normalizar_descarta_fuera_de_dominios_y_sin_datos():
    dom = ["amazon.com.mx"]
    # host no esta entre los dominios buscados
    assert competencia._normalizar(
        {"titulo": "x", "url": "https://mercadolibre.com.mx/y"}, dom) is None
    # sin titulo
    assert competencia._normalizar(
        {"titulo": "", "url": "https://amazon.com.mx/z"}, dom) is None


# ------------------------- integracion: PATCH sitios ---------------------------

def test_patch_competencia_sitios_normaliza_y_persiste(cliente, db_session):
    cat = Categoria(slug="camas", orden=10)
    db_session.add(cat)
    db_session.commit()

    r = cliente.patch(
        f"/api/catalogo/categorias/{cat.id}/competencia-sitios",
        json={"competencia_sitios": "HTTPS://www.Amazon.com.mx/dp/1, petco.com.mx  petco.com.mx"},
    )
    assert r.status_code == 200
    assert r.json()["competencia_sitios"] == "amazon.com.mx,petco.com.mx"

    # aparece serializado en el listado
    lst = cliente.get("/api/catalogo/categorias").json()["items"]
    fila = next(c for c in lst if c["slug"] == "camas")
    assert fila["competencia_sitios"] == "amazon.com.mx,petco.com.mx"

    # vaciar => None
    r2 = cliente.patch(
        f"/api/catalogo/categorias/{cat.id}/competencia-sitios",
        json={"competencia_sitios": "   "},
    )
    assert r2.status_code == 200
    assert r2.json()["competencia_sitios"] is None


# --------------------- integracion: wiring del endpoint buscar -----------------

def _prod_con_categoria(db_session, slug, sitios):
    prov = Proveedor(nombre="V1", archivo_pdf="v1.pdf")
    db_session.add(prov)
    db_session.commit()
    prod = Producto(proveedor_id=prov.id, sku="A1", descripcion="cama para perro",
                    material="tela", medidas="60x40", categoria=slug)
    db_session.add(prod)
    if slug is not None:
        db_session.add(Categoria(slug=slug, orden=10, competencia_sitios=sitios))
    db_session.commit()
    return prod


def test_buscar_usa_sitios_categoria_mas_extra(cliente, db_session, monkeypatch):
    prod = _prod_con_categoria(db_session, "camas", "amazon.com.mx,petco.com.mx")
    capturado = {}

    def fake_buscar(query, dominios=None):
        capturado["query"] = query
        capturado["dominios"] = dominios
        return {"ok": True, "candidatos": [], "query": query, "modelo": "x", "error": None}

    monkeypatch.setattr(competencia, "buscar_candidatos", fake_buscar)

    r = cliente.post(f"/api/productos/{prod.id}/competencia/buscar",
                     json={"sitios_extra": "https://petsy.mx/x"})
    assert r.status_code == 200
    assert capturado["dominios"] == ["amazon.com.mx", "petco.com.mx", "petsy.mx"]
    # sin query en el body => se arma desde el producto
    assert "cama para perro" in capturado["query"]


def test_buscar_sin_config_categoria_cae_a_default(cliente, db_session, monkeypatch):
    # producto con categoria sin fila Categoria => defaults
    prov = Proveedor(nombre="V", archivo_pdf="v.pdf")
    db_session.add(prov)
    db_session.commit()
    prod = Producto(proveedor_id=prov.id, sku="Z", descripcion="jaula", categoria="rejas")
    db_session.add(prod)
    db_session.commit()
    capturado = {}
    monkeypatch.setattr(competencia, "buscar_candidatos",
                        lambda q, dominios=None: capturado.update(dominios=dominios) or
                        {"ok": True, "candidatos": [], "query": q, "modelo": "x", "error": None})

    r = cliente.post(f"/api/productos/{prod.id}/competencia/buscar", json={})
    assert r.status_code == 200
    assert capturado["dominios"] == ["amazon.com.mx", "mercadolibre.com.mx", "petco.com.mx"]


# --------------------------- unit: resumen dinamico ----------------------------

def test_resumen_competencia_dinamico():
    from app.routes import _resumen_competencia

    def lst(mp, precio):
        return types.SimpleNamespace(marketplace=mp, precio_mxn=precio)

    items = [
        lst("petco_mx", 200), lst("amazon_mx", 100), lst("amazon_mx", 300),
        lst("petsy.mx", 150),
    ]
    res = _resumen_competencia(items)
    claves = list(res.keys())
    # canonicos presentes primero (en su orden), luego extra, global al final
    assert claves == ["amazon_mx", "petco_mx", "petsy.mx", "global"]
    assert res["amazon_mx"]["min"] == 100 and res["amazon_mx"]["max"] == 300
    assert res["petsy.mx"]["n"] == 1
    assert res["global"]["n"] == 4
