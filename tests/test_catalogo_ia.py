"""Tests del bootstrapping de catalogo asistido por IA.

No golpean la API de Anthropic: la fase de red se monkeypatchea. Cubren:
- helpers puros de app/catalogo_ia (slugify, muestreo, extraccion JSON, compose).
- endpoints aplicar-propuesta / arancel (persistencia + estados).
- resolver_arancel usando la fraccion confirmada a nivel categoria.
- semantica del clasificador: tabla existente pero vacia -> reglas vacias (NO
  fallback al YAML de mascotas).
"""

import pytest
from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import crear_app
from app.modelos import Base, Proveedor, Producto, Categoria, CategoriaKeyword
from app.cotizador.lookup import resolver_arancel
import app.catalogo_ia as cia


@pytest.fixture
def cliente(db_session):
    from app.routes import get_db
    app = crear_app()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


# --------------------------- helpers puros ---------------------------------

def test_slugify():
    assert cia._slugify("Casa / Jaula Perros") == "casa-jaula-perros"
    assert cia._slugify("Termos & Vasos") == "termos-vasos"
    assert cia._slugify("Árboles Rascadores") == "arboles-rascadores"
    assert cia._slugify("") == ""


def test_extraer_json_objeto_y_arreglo():
    fence = chr(96) * 3
    obj = "ruido " + fence + "json\n" + '{"dominio":"cocina","categorias":[]}' + "\n" + fence
    assert cia._extraer_json(obj) == {"dominio": "cocina", "categorias": []}
    assert cia._extraer_json('pre [ {"slug":"a"} ] post') == [{"slug": "a"}]
    assert cia._extraer_json("sin json") is None


def test_muestreo_dedup_y_cap(monkeypatch):
    monkeypatch.setattr(cia, "MAX_DESCRIPCIONES", 3)
    descs = ["Plastic bowl", "plastic BOWL", "x", "Cat tunnel", "Dog leash", "Bird cage", "Water bottle"]
    muestra, total = cia._muestrear_descripciones(descs)
    assert total == 5  # dedup case-insensitive + descarta 'x' (<5 chars)
    assert len(muestra) == 3


def test_proponer_categorias_reutiliza_catalogo_existente(monkeypatch):
    """La propuesta inyecta las categorias existentes en el prompt (mejora anti-duplicados)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    capturado = {}

    class _Resp:
        content = [type("B", (), {"type": "text",
                   "text": '{"dominio":"flores","categorias":[{"slug":"ramos-rosas",'
                           '"nombre":"Rosas","orden":10,"keywords":["rosa"]}]}'})()]

    class _Client:
        def __init__(self, *a, **k):
            pass

        class messages:
            @staticmethod
            def create(**kw):
                capturado["prompt"] = kw["messages"][0]["content"]
                return _Resp()

    monkeypatch.setattr(cia.anthropic, "Anthropic", _Client)
    r = cia.proponer_categorias(
        ["Ramo de rosas rojas artificiales de seda"],
        categorias_existentes=[{"slug": "ramos-rosas", "keywords": ["rosa", "rose"]}],
    )
    assert r["ok"]
    assert "ramos-rosas" in capturado["prompt"]
    assert "REUTILIZA" in capturado["prompt"]


def test_proponer_catalogo_compone_estados(monkeypatch):
    """Con fase1+fase2 monkeypatcheadas: fraccion valida -> propuesto; sin -> pendiente."""
    monkeypatch.setattr(cia, "proponer_categorias", lambda descs, categorias_existentes=None: {
        "ok": True, "error": None, "modelo": "m", "dominio": "cocina",
        "categorias": [
            {"slug": "termos", "nombre": "Termos", "orden": 30, "keywords": ["termo"]},
            {"slug": "vasos", "nombre": "Vasos", "orden": 40, "keywords": ["vaso"]},
        ],
    })
    monkeypatch.setattr(cia, "investigar_aranceles", lambda cats: {
        "ok": True, "error": None, "modelo": "m", "resultados": {
            "termos": {"fraccion": "9617.00.01", "tasa_pct": 15, "confianza": 0.9,
                       "fuente_url": "http://x", "nota": "ok"},
            "vasos": {"fraccion": None, "tasa_pct": None, "confianza": 0.2,
                      "fuente_url": None, "nota": "no seguro"},
        },
    })
    prop = cia.proponer_catalogo(["Termo acero", "Vaso vidrio"])
    assert prop["ok"]
    by = {c["slug"]: c for c in prop["categorias"]}
    assert by["termos"]["arancel_estado"] == "propuesto"
    assert by["termos"]["fraccion"] == "9617.00.01"
    assert by["vasos"]["arancel_estado"] == "pendiente"
    assert by["vasos"]["fraccion"] is None


def test_proponer_catalogo_confianza_baja_es_pendiente(monkeypatch):
    monkeypatch.setattr(cia, "proponer_categorias", lambda descs, categorias_existentes=None: {
        "ok": True, "error": None, "modelo": "m", "dominio": "x",
        "categorias": [{"slug": "sartenes", "nombre": "Sartenes", "orden": 20, "keywords": ["pan"]}],
    })
    monkeypatch.setattr(cia, "investigar_aranceles", lambda cats: {
        "ok": True, "error": None, "modelo": "m", "resultados": {
            "sartenes": {"fraccion": "7323.99.99", "tasa_pct": 15, "confianza": 0.2,
                         "fuente_url": None, "nota": "duda"},
        },
    })
    prop = cia.proponer_catalogo(["sarten"])
    assert prop["categorias"][0]["arancel_estado"] == "pendiente"


# --------------------------- endpoints -------------------------------------

def test_aplicar_propuesta_persiste_estados(cliente, db_session):
    body = {"items": [
        {"slug": "termos", "orden": 30, "keywords": ["termo", "thermos"],
         "fraccion": "9617.00.01", "tasa_pct": 15, "arancel_nota": "vacuum"},
        {"slug": "misc", "orden": 200, "keywords": ["cosa"],
         "fraccion": None, "tasa_pct": None},
    ]}
    r = cliente.post("/api/catalogo/aplicar-propuesta", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["creadas"] == 2
    assert data["confirmadas"] == 1
    assert data["pendientes"] == 1
    assert data["reclasificar"] is True

    termos = db_session.query(Categoria).filter_by(slug="termos").first()
    assert termos.arancel_estado == "confirmado"
    assert termos.fraccion == "9617.00.01"
    assert termos.tasa_pct == 15
    assert {kw.keyword for kw in termos.keywords} == {"termo", "thermos"}
    misc = db_session.query(Categoria).filter_by(slug="misc").first()
    assert misc.arancel_estado == "pendiente"


def test_aplicar_propuesta_fraccion_invalida_queda_pendiente(cliente, db_session):
    body = {"items": [
        {"slug": "raras", "orden": 50, "keywords": [], "fraccion": "96170001", "tasa_pct": 10},
    ]}
    r = cliente.post("/api/catalogo/aplicar-propuesta", json=body)
    assert r.status_code == 200
    assert r.json()["pendientes"] == 1
    cat = db_session.query(Categoria).filter_by(slug="raras").first()
    assert cat.arancel_estado == "pendiente"
    assert cat.fraccion is None  # formato invalido descartado


def test_listar_categorias_incluye_arancel(cliente, db_session):
    cliente.post("/api/catalogo/aplicar-propuesta", json={"items": [
        {"slug": "termos", "orden": 30, "keywords": ["termo"],
         "fraccion": "9617.00.01", "tasa_pct": 15},
    ]})
    r = cliente.get("/api/catalogo/categorias")
    assert r.status_code == 200
    item = next(i for i in r.json()["items"] if i["slug"] == "termos")
    assert item["fraccion"] == "9617.00.01"
    assert item["arancel_estado"] == "confirmado"


def test_patch_arancel_categoria(cliente, db_session):
    cliente.post("/api/catalogo/aplicar-propuesta", json={"items": [
        {"slug": "pendiente-cat", "orden": 60, "keywords": ["algo"]},
    ]})
    cat = db_session.query(Categoria).filter_by(slug="pendiente-cat").first()
    r = cliente.patch(f"/api/catalogo/categorias/{cat.id}/arancel",
                      json={"fraccion": "3924.10.01", "tasa_pct": 15})
    assert r.status_code == 200
    db_session.refresh(cat)
    assert cat.arancel_estado == "confirmado"
    assert cat.fraccion == "3924.10.01"
    # fraccion invalida -> 422
    r2 = cliente.patch(f"/api/catalogo/categorias/{cat.id}/arancel",
                       json={"fraccion": "malformada", "tasa_pct": 15})
    assert r2.status_code == 422


def test_proponer_ia_sin_productos_es_400(cliente):
    r = cliente.post("/api/catalogo/proponer-ia")
    assert r.status_code == 400


def test_pagina_categorias_renderiza(cliente, db_session):
    """La plantilla /categorias renderiza (protege contra errores de Jinja)."""
    r = cliente.get("/categorias")
    assert r.status_code == 200
    assert "Proponer catalogo con IA" in r.text
    assert "proponerCatalogoIA" in r.text
    assert "Describir fotos con IA" in r.text
    assert "describirFotosIA" in r.text


# --------------------------- vision (describir fotos) ----------------------

def test_describir_fotos_vacio_sin_api():
    r = cia.describir_fotos([])
    assert r["ok"] is True
    assert r["resultados"] == {}


def test_describir_fotos_compose(monkeypatch):
    """Con _describir_batch monkeypatcheado: agrega resultados por lote con tags."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cia, "VISION_BATCH", 2)

    lotes = []

    def fake_batch(batch, client):
        lotes.append(len(batch))
        return {it["producto_id"]: {"descripcion": f"desc {it['producto_id']}",
                                    "tags": ["rosa", "artificial"]}
                for it in batch}

    monkeypatch.setattr(cia, "_describir_batch", fake_batch)
    items = [{"producto_id": i, "sku": f"S{i}", "medidas": "", "path": "x"} for i in range(5)]
    r = cia.describir_fotos(items)
    assert r["ok"] is True
    assert len(r["resultados"]) == 5
    assert r["resultados"][3]["descripcion"] == "desc 3"
    assert r["resultados"][3]["tags"] == ["rosa", "artificial"]
    # 5 items en lotes de 2 -> [2,2,1]
    assert lotes == [2, 2, 1]


def test_necesita_descripcion():
    from app.routes import _necesita_descripcion
    assert _necesita_descripcion("") is True
    assert _necesita_descripcion(None) is True
    assert _necesita_descripcion("   ") is True
    assert _necesita_descripcion("Product 223") is True
    assert _necesita_descripcion("product 5") is True
    assert _necesita_descripcion("Ramo de rosas artificiales") is False
    assert _necesita_descripcion("Producto especial 3") is False  # no es placeholder exacto


def test_sin_descripcion_filtro_captura_vacias_y_placeholder(db_session):
    """El filtro SQL captura descripcion vacia + placeholder 'Product N', no reales."""
    from app.routes import _sin_descripcion_filtro
    pv = Proveedor(nombre="V")
    db_session.add(pv)
    db_session.commit()
    for sku, desc in [("A", ""), ("B", "Product 42"), ("C", "Ramo de peonias"), ("D", "product 7")]:
        db_session.add(Producto(proveedor_id=pv.id, sku=sku, descripcion=desc, fob_usd=1.0))
    db_session.commit()
    skus = {
        p.sku for p in db_session.query(Producto).filter(_sin_descripcion_filtro()).all()
    }
    assert skus == {"A", "B", "D"}  # C ('Ramo de peonias') queda fuera


def test_describir_batch_normaliza_tags_e_indices(monkeypatch):
    """_describir_batch mapea por indice 'i' y normaliza tags (lower/dedup)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class _Resp:
        content = [type("B", (), {"type": "text",
                   "text": '[{"i":0,"descripcion":"Rosa roja","tags":["Rosa","ROSA","roja"]},'
                           '{"i":9,"descripcion":"fuera de rango","tags":[]}]'})()]

    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp()

    batch = [{"producto_id": 100, "sku": "A", "medidas": "10cm", "path": "nope.png"}]
    out = cia._describir_batch(batch, _Client())
    assert out[100]["descripcion"] == "Rosa roja"
    assert out[100]["tags"] == ["rosa", "roja"]  # lower + dedup
    assert 9 not in out  # indice fuera de rango ignorado


# --------------------------- resolver_arancel ------------------------------

def test_resolver_arancel_usa_categoria_confirmada(db_session):
    db_session.add(Categoria(slug="termos", orden=30, fraccion="9617.00.01",
                             tasa_pct=15, arancel_estado="confirmado"))
    db_session.commit()
    res = resolver_arancel(db_session, "termos", None, "plastico")
    assert res.fuente == "categoria-confirmada"
    assert res.fraccion == "9617.00.01"
    assert res.tasa_pct == Decimal("15")


def test_resolver_arancel_pendiente_cae_a_default(db_session):
    db_session.add(Categoria(slug="misc", orden=200, arancel_estado="pendiente"))
    db_session.commit()
    res = resolver_arancel(db_session, "misc", None, "plastico")
    # sin fraccion confirmada -> default 25% (no 'categoria-confirmada')
    assert res.fuente == "default-25"
    assert res.tasa_pct == Decimal("25")


def test_resolver_arancel_confirmada_vence_a_metal(db_session):
    db_session.add(Categoria(slug="sartenes", orden=20, fraccion="7323.99.99",
                             tasa_pct=10, arancel_estado="confirmado"))
    db_session.commit()
    # material metalico normalmente daria 35%; la categoria confirmada gana
    res = resolver_arancel(db_session, "sartenes", None, "stainless steel")
    assert res.fuente == "categoria-confirmada"
    assert res.tasa_pct == Decimal("10")


# --------------------------- clasificador (tabla vacia) --------------------

def test_clasificador_tabla_vacia_no_cae_a_yaml(tmp_path, monkeypatch):
    """Un proyecto con tabla 'categorias' existente pero VACIA no debe heredar
    las categorias de mascotas del YAML."""
    import app.clasificador as clf
    from app import db as db_module

    db_path = tmp_path / "vacio.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)  # crea categorias (vacia)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "get_session_factory", lambda slug=None: Session)

    clf.invalidar_cache()
    try:
        reglas = clf._intentar_bd("proyecto-vacio")
        assert reglas is not None            # tabla existe -> no None
        assert reglas.categorias == []       # sin categorias (no mascotas)
        # 'kennel' caeria en 'casa-jaula' con el YAML; aqui debe ser None
        assert clf.clasificar_descripcion("Plastic pet kennel dog house", "proyecto-vacio") is None
    finally:
        clf.invalidar_cache()
