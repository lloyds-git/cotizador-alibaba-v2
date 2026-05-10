# Base de Datos de Productos Mascotas - Plan de Implementacion

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Construir una BD SQLite que centralice todos los productos extraidos de PDFs de cotizacion, con UI web local para revisar/marcar candidatos a cotizar y regenerar el formato HD con los seleccionados.

**Architecture:**
- **Backend:** Python 3.14 + FastAPI + SQLAlchemy + SQLite.
- **Storage:** `productos.db` (SQLite) con tablas `proveedores`, `productos`, `productos_fotos`, `cotizaciones_marcadas`. Fotos referenciadas a archivos en `data/fotos/` (no blobs).
- **Pipeline ingest:** Script `ingestar_pdf.py` que reusa `pdf_a_formato_hd.py` (sin reescribir Adobe/Haiku), inserta cada producto en la BD con SHA hash para deduplicacion.
- **UI:** Frontend simple con HTMX + Tailwind CDN servido desde FastAPI. Una tabla con foto+datos+precios calculados, filtros, edicion inline, boton "Marcar para cotizar".
- **Export:** Endpoint que genera `formato-hd-XXX.xlsx` con los productos marcados, reusando `llenar_formato_hd.py`.

**Tech Stack:**
- Python 3.14 (ya instalado)
- FastAPI 0.115+, Uvicorn (ASGI), SQLAlchemy 2.x, Pydantic 2.x
- SQLite (built-in en Python)
- HTMX 1.9 + Alpine.js + Tailwind CDN (sin build step)
- openpyxl, pywin32, anthropic (ya usado en `extraer_con_claude.py`)
- pytest + httpx para tests

**Directorio de trabajo:** `C:\Users\salomon.DC0\Documents\Mascotas-9Mayo\`

---

## Pre-requisitos antes de empezar

**Limpieza inicial:** Borra/archiva la BD anterior contaminada. Si existe `productos.db` o similar en la raiz, muevelo a `docs/archivo/` con sufijo `_v1`.

**Estructura objetivo del proyecto:**
```
Mascotas-9Mayo/
├── llenar_formato_hd.py        # YA EXISTE - no tocar
├── extraer_con_claude.py       # YA EXISTE - no tocar
├── pdf_a_formato_hd.py         # YA EXISTE - se llama desde ingestar
├── Formato HD-Mascotas.xlsb    # YA EXISTE
├── .env                         # YA EXISTE
├── app/                         # NUEVO - codigo de la app
│   ├── __init__.py
│   ├── db.py                    # SQLAlchemy engine + session
│   ├── modelos.py               # tablas ORM
│   ├── pricing.py               # logica de precios calculados
│   ├── ingest.py                # importar de _intermedio_*.xlsx + carpetas adobe
│   ├── exportar.py              # productos seleccionados -> formato HD
│   ├── main.py                  # FastAPI app
│   ├── routes.py                # endpoints + HTML
│   └── templates/
│       ├── base.html
│       ├── productos.html
│       └── _fila.html
├── data/
│   ├── productos.db             # SQLite
│   └── fotos/                   # PNGs copiados desde _adobe_extract_*
├── tests/
│   ├── conftest.py
│   ├── test_modelos.py
│   ├── test_pricing.py
│   ├── test_ingest.py
│   ├── test_api.py
│   └── test_exportar.py
└── docs/plans/                  # este plan
```

---

## Task 1: Setup del proyecto y dependencias

**Files:**
- Create: `app/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `pyproject.toml`
- Create: `.gitignore` (anadir a la raiz si no existe)

**Step 1: Verificar Python y crear estructura**

Run:
```bash
cd "c:/Users/salomon.DC0/Documents/Mascotas-9Mayo"
python --version  # debe ser 3.14
mkdir -p app/templates tests data/fotos docs/plans
```

**Step 2: Crear `pyproject.toml`**

```toml
[project]
name = "mascotas-bd"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "sqlalchemy>=2.0",
    "pydantic>=2.0",
    "jinja2>=3.1",
    "openpyxl>=3.1",
    "python-dotenv>=1.0",
    "anthropic>=0.40",
    "pywin32>=306; sys_platform == 'win32'",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "httpx>=0.27",
    "pytest-asyncio>=0.23",
]
```

**Step 3: Instalar dependencias**

Run:
```bash
pip install -e ".[dev]"
```

Expected: instalacion exitosa, no errores.

**Step 4: Crear `tests/conftest.py` con fixture de BD temporal**

```python
import pytest
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

@pytest.fixture
def db_session(tmp_path):
    """Fixture que da una sesion de BD aislada por test."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    from app.modelos import Base
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
```

**Step 5: Crear `.gitignore`**

```
data/*.db
data/fotos/
_adobe_extract_*/
_intermedio_*.xlsx
formato-hd-*.xlsx
__pycache__/
*.pyc
.env
.pytest_cache/
*.egg-info/
```

**Step 6: Commit**

```bash
git add app/ tests/ pyproject.toml .gitignore docs/plans/
git commit -m "feat(bd): inicializar estructura del proyecto y dependencias"
```

---

## Task 2: Modelos de BD (TDD)

**Files:**
- Create: `tests/test_modelos.py`
- Create: `app/modelos.py`
- Create: `app/db.py`

**Step 1: Escribir test que falla**

Crear `tests/test_modelos.py`:

```python
from app.modelos import Proveedor, Producto, Foto


def test_crear_proveedor(db_session):
    p = Proveedor(
        nombre="Zhejiang Xinding Plastic",
        archivo_pdf="Xinding_Quotation_20260422.pdf",
    )
    db_session.add(p)
    db_session.commit()
    assert p.id > 0
    assert p.nombre == "Zhejiang Xinding Plastic"


def test_crear_producto_con_proveedor(db_session):
    prov = Proveedor(nombre="Test", archivo_pdf="t.pdf")
    db_session.add(prov)
    db_session.commit()

    prod = Producto(
        proveedor_id=prov.id,
        sku="XDB-490M1",
        descripcion="Plastic pet kennel Medium",
        fob_usd=12.5,
        material="PP",
        medidas="L750xW640xH516 mm",
        moq="300 pcs",
        cbm=0.07,
        pzas_20ft=380,
        marcado_cotizar=False,
    )
    db_session.add(prod)
    db_session.commit()
    assert prod.id > 0
    assert prod.proveedor.nombre == "Test"


def test_sku_unico_por_proveedor(db_session):
    """Mismo SKU del mismo proveedor no se duplica."""
    prov = Proveedor(nombre="P", archivo_pdf="p.pdf")
    db_session.add(prov)
    db_session.commit()

    db_session.add(Producto(proveedor_id=prov.id, sku="A", descripcion="d1"))
    db_session.commit()

    # Intentar agregar el mismo sku -> debe fallar
    from sqlalchemy.exc import IntegrityError
    import pytest
    db_session.add(Producto(proveedor_id=prov.id, sku="A", descripcion="d2"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_producto_con_fotos(db_session):
    prov = Proveedor(nombre="P", archivo_pdf="p.pdf")
    db_session.add(prov)
    db_session.commit()

    prod = Producto(proveedor_id=prov.id, sku="A", descripcion="d")
    db_session.add(prod)
    db_session.commit()

    foto = Foto(producto_id=prod.id, ruta_relativa="fotos/A_1.png", es_principal=True)
    db_session.add(foto)
    db_session.commit()

    assert len(prod.fotos) == 1
    assert prod.fotos[0].es_principal
```

**Step 2: Correr para confirmar que fallan**

Run: `pytest tests/test_modelos.py -v`
Expected: 4 tests FAIL con "ModuleNotFoundError: app.modelos"

**Step 3: Implementar modelos**

Crear `app/modelos.py`:

```python
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, ForeignKey, DateTime,
    UniqueConstraint, Text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Proveedor(Base):
    __tablename__ = "proveedores"
    id = Column(Integer, primary_key=True)
    nombre = Column(String(200), nullable=False)
    archivo_pdf = Column(String(500))
    pais = Column(String(50), default="China")
    contacto = Column(String(200))
    creado_en = Column(DateTime, default=datetime.utcnow)

    productos = relationship("Producto", back_populates="proveedor")


class Producto(Base):
    __tablename__ = "productos"
    __table_args__ = (
        UniqueConstraint("proveedor_id", "sku", name="uq_proveedor_sku"),
    )

    id = Column(Integer, primary_key=True)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), nullable=False)
    sku = Column(String(50))
    descripcion = Column(Text, nullable=False)

    # Datos extraidos
    fob_usd = Column(Float)  # precio FOB en dolares
    material = Column(String(100))
    medidas = Column(String(200))
    peso_kg = Column(Float)
    color = Column(String(200))
    moq = Column(String(50))
    packing = Column(String(200))
    carton_dims = Column(String(200))
    cbm = Column(Float)
    pzas_20ft = Column(Integer)
    pzas_40hq = Column(Integer)
    lead_time = Column(String(100))

    # Estado de cotizacion
    marcado_cotizar = Column(Boolean, default=False, nullable=False)
    notas = Column(Text)

    creado_en = Column(DateTime, default=datetime.utcnow)
    actualizado_en = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    proveedor = relationship("Proveedor", back_populates="productos")
    fotos = relationship("Foto", back_populates="producto", cascade="all, delete-orphan")


class Foto(Base):
    __tablename__ = "fotos"
    id = Column(Integer, primary_key=True)
    producto_id = Column(Integer, ForeignKey("productos.id"), nullable=False)
    ruta_relativa = Column(String(500), nullable=False)  # ej "fotos/XDB-490_1.png"
    es_principal = Column(Boolean, default=False)

    producto = relationship("Producto", back_populates="fotos")
```

**Step 4: Crear `app/db.py`**

```python
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.modelos import Base

DB_PATH = Path(__file__).parent.parent / "data" / "productos.db"


def get_engine():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{DB_PATH}", echo=False)


def get_session_factory():
    return sessionmaker(bind=get_engine())


def init_db():
    """Crea las tablas si no existen."""
    Base.metadata.create_all(get_engine())
```

**Step 5: Correr tests**

Run: `pytest tests/test_modelos.py -v`
Expected: 4 tests PASS.

**Step 6: Commit**

```bash
git add app/modelos.py app/db.py tests/test_modelos.py
git commit -m "feat(bd): modelos Proveedor, Producto, Foto con tests"
```

---

## Task 3: Logica de pricing (TDD)

**Files:**
- Create: `tests/test_pricing.py`
- Create: `app/pricing.py`

**Step 1: Escribir tests primero**

```python
from app.pricing import (
    calcular_landed_mxn,
    calcular_retail_mxn,
    calcular_margen_hd,
)


def test_landed_mxn_basico():
    # FOB 10 USD, tipo cambio 20, factor importacion 1.4 -> 280 MXN
    r = calcular_landed_mxn(fob_usd=10.0, tipo_cambio=20.0, factor_importacion=1.4)
    assert r == 280.0


def test_landed_mxn_redondeado():
    r = calcular_landed_mxn(fob_usd=8.0, tipo_cambio=20.5, factor_importacion=1.35)
    assert r == round(8.0 * 20.5 * 1.35, 2)


def test_retail_sugerido():
    # landed 280, margen objetivo 60% -> retail 700 (sin IVA), con IVA 812
    r = calcular_retail_mxn(landed_mxn=280.0, margen_objetivo=0.60, iva=0.16)
    assert r == 812.0


def test_margen_hd():
    # costo 280, retail con IVA 812 -> margen = 1 - 280/(812/1.16) = 1 - 0.4 = 0.6
    m = calcular_margen_hd(costo=280.0, retail_con_iva=812.0, iva=0.16)
    assert round(m, 4) == 0.6


def test_pricing_completo_para_producto():
    """Producto con FOB 12.5 USD -> debe dar precios coherentes."""
    landed = calcular_landed_mxn(12.5, 20.0, 1.4)
    retail = calcular_retail_mxn(landed, 0.40, 0.16)
    margen = calcular_margen_hd(landed, retail, 0.16)
    # Verificar que el margen retornado matchea el objetivo
    assert round(margen, 2) == 0.40
```

**Step 2: Correr para verificar fallo**

Run: `pytest tests/test_pricing.py -v`
Expected: 5 tests FAIL.

**Step 3: Implementar `app/pricing.py`**

```python
"""
Logica de calculo de precios para productos importados.

Flujo: FOB USD -> Landed MXN -> Retail MXN -> Margen HD

Parametros tipicos:
- factor_importacion: 1.30 a 1.45 (incluye flete, aduana, impuestos, manejo)
- margen_objetivo: 0.35 a 0.65 segun categoria
- IVA Mexico: 0.16
"""

from __future__ import annotations


def calcular_landed_mxn(
    fob_usd: float,
    tipo_cambio: float,
    factor_importacion: float,
) -> float:
    """
    Calcula el costo landed en pesos mexicanos.

    fob_usd * tipo_cambio * factor_importacion
    """
    return round(fob_usd * tipo_cambio * factor_importacion, 2)


def calcular_retail_mxn(
    landed_mxn: float,
    margen_objetivo: float,
    iva: float = 0.16,
) -> float:
    """
    Calcula el precio retail (con IVA) para alcanzar un margen objetivo.

    margen = 1 - costo/(retail_sin_iva)
    retail_sin_iva = costo / (1 - margen)
    retail_con_iva = retail_sin_iva * (1 + iva)
    """
    if margen_objetivo >= 1.0 or margen_objetivo < 0:
        raise ValueError("margen_objetivo debe estar entre 0 y <1")
    retail_sin_iva = landed_mxn / (1 - margen_objetivo)
    retail_con_iva = retail_sin_iva * (1 + iva)
    return round(retail_con_iva, 2)


def calcular_margen_hd(
    costo: float,
    retail_con_iva: float,
    iva: float = 0.16,
) -> float:
    """
    Calcula el margen efectivo dado un costo y un retail con IVA.
    """
    if retail_con_iva <= 0:
        return 0.0
    retail_sin_iva = retail_con_iva / (1 + iva)
    return 1 - costo / retail_sin_iva
```

**Step 4: Correr tests**

Run: `pytest tests/test_pricing.py -v`
Expected: 5 tests PASS.

**Step 5: Commit**

```bash
git add app/pricing.py tests/test_pricing.py
git commit -m "feat(pricing): funciones landed/retail/margen con tests"
```

---

## Task 4: Ingest desde xlsx intermedios (TDD)

**Files:**
- Create: `tests/test_ingest.py`
- Create: `tests/fixtures/_intermedio_demo.xlsx`
- Create: `app/ingest.py`

**Step 1: Crear fixture xlsx demo**

Crear un mini xlsx de prueba con 2 filas + 1 foto incrustada usando un script de setup. Crear `tests/fixtures/crear_fixture.py`:

```python
"""Script one-shot para crear el xlsx de prueba. Correr una vez."""
import openpyxl
from openpyxl.drawing.image import Image
from pathlib import Path

ROOT = Path(__file__).parent
ROOT.mkdir(parents=True, exist_ok=True)

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Cotizacion PDF"

# Headers iguales a los que produce construir_xlsx_desde_claude
headers = ["Foto", "SKU", "Descripcion", "Medidas", "Material", "Peso (kg)",
           "Color", "MOQ", "Packing", "Carton dims", "CBM",
           "Pzas 20ft", "Pzas 40hq", "Lead time", "FOB USD"]
for i, h in enumerate(headers, 1):
    ws.cell(row=1, column=i, value=h)

# Dos productos
ws.cell(row=2, column=2, value="TEST-001")
ws.cell(row=2, column=3, value="Producto demo grande")
ws.cell(row=2, column=4, value="L100xW50xH30 mm")
ws.cell(row=2, column=5, value="PP")
ws.cell(row=2, column=6, value="2.5")
ws.cell(row=2, column=8, value="300 pcs")
ws.cell(row=2, column=11, value="0.05")
ws.cell(row=2, column=12, value="500")
ws.cell(row=2, column=15, value=10.5)

ws.cell(row=3, column=2, value="TEST-002")
ws.cell(row=3, column=3, value="Producto demo chico")
ws.cell(row=3, column=15, value=5.0)

# Foto demo (1x1 PNG)
import io
png_bytes = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4"
    "890000000D49444154789C636400000000050001A5F645620000000049454E44AE426082"
)
foto_path = ROOT / "demo.png"
foto_path.write_bytes(png_bytes)
img = Image(str(foto_path))
img.anchor = "A2"
ws.add_image(img)

wb.save(ROOT / "_intermedio_demo.xlsx")
print(f"Creado: {ROOT / '_intermedio_demo.xlsx'}")
```

Correr una vez:
```bash
python tests/fixtures/crear_fixture.py
```

**Step 2: Escribir tests**

`tests/test_ingest.py`:

```python
from pathlib import Path
from app.ingest import ingestar_xlsx_intermedio


def test_ingest_inserta_proveedor_y_productos(db_session, tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "_intermedio_demo.xlsx"
    fotos_dir = tmp_path / "fotos"

    n = ingestar_xlsx_intermedio(
        session=db_session,
        xlsx_path=str(fixture),
        nombre_proveedor="Demo Vendor",
        fotos_destino=str(fotos_dir),
    )
    db_session.commit()
    assert n == 2

    from app.modelos import Proveedor, Producto
    prov = db_session.query(Proveedor).filter_by(nombre="Demo Vendor").first()
    assert prov is not None
    assert len(prov.productos) == 2

    p1 = db_session.query(Producto).filter_by(sku="TEST-001").first()
    assert p1.fob_usd == 10.5
    assert p1.material == "PP"
    assert p1.cbm == 0.05
    assert p1.pzas_20ft == 500
    assert len(p1.fotos) == 1
    # Verifica que copiamos la foto a destino
    foto_path = fotos_dir / Path(p1.fotos[0].ruta_relativa).name
    assert foto_path.exists()


def test_ingest_idempotente(db_session, tmp_path):
    """Re-ingerir el mismo xlsx no duplica productos."""
    fixture = Path(__file__).parent / "fixtures" / "_intermedio_demo.xlsx"
    fotos_dir = tmp_path / "fotos"

    ingestar_xlsx_intermedio(db_session, str(fixture), "V", str(fotos_dir))
    db_session.commit()
    n2 = ingestar_xlsx_intermedio(db_session, str(fixture), "V", str(fotos_dir))
    db_session.commit()
    assert n2 == 0  # ya estaban, no inserta de nuevo
    from app.modelos import Producto
    assert db_session.query(Producto).count() == 2


def test_ingest_actualiza_fob_si_cambio(db_session, tmp_path):
    """Si el precio cambio, debe actualizar el registro existente."""
    fixture = Path(__file__).parent / "fixtures" / "_intermedio_demo.xlsx"
    fotos_dir = tmp_path / "fotos"

    ingestar_xlsx_intermedio(db_session, str(fixture), "V", str(fotos_dir))
    db_session.commit()

    from app.modelos import Producto
    p = db_session.query(Producto).filter_by(sku="TEST-001").first()
    p.fob_usd = 99.0  # simular precio viejo
    db_session.commit()

    # Re-ingest debe restaurar el FOB del xlsx
    ingestar_xlsx_intermedio(db_session, str(fixture), "V", str(fotos_dir))
    db_session.commit()

    p2 = db_session.query(Producto).filter_by(sku="TEST-001").first()
    assert p2.fob_usd == 10.5
```

**Step 3: Correr tests para verificar fallo**

Run: `pytest tests/test_ingest.py -v`
Expected: 3 FAIL con `ModuleNotFoundError: app.ingest`

**Step 4: Implementar `app/ingest.py`**

```python
"""
Ingest de productos desde xlsx intermedios (los que produce pdf_a_formato_hd.py
en el paso 3) a la BD SQLite.

Estrategia:
- Idempotente: si (proveedor, sku) ya existe, actualiza datos en vez de duplicar.
- Copia las imagenes desde donde esten al directorio data/fotos/.
- No toca el campo marcado_cotizar al actualizar (preserva eleccion del usuario).
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import openpyxl
from sqlalchemy.orm import Session

from app.modelos import Proveedor, Producto, Foto


# Columnas esperadas en el xlsx intermedio (mismo layout que
# construir_xlsx_desde_claude en pdf_a_formato_hd.py)
COL_FOTO = 1
COL_SKU = 2
COL_DESC = 3
COL_MEDIDAS = 4
COL_MATERIAL = 5
COL_PESO = 6
COL_COLOR = 7
COL_MOQ = 8
COL_PACKING = 9
COL_CARTON = 10
COL_CBM = 11
COL_PZAS20 = 12
COL_PZAS40 = 13
COL_LEAD = 14
COL_FOB = 15


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def _to_int(v) -> int | None:
    f = _to_float(v)
    return int(f) if f is not None else None


def _extraer_imagenes_xlsx(xlsx_path: str) -> dict[int, bytes]:
    """
    Devuelve {fila_excel: bytes_de_imagen} para imagenes ancladas en col A.
    Lee el ZIP del xlsx directamente (no usa openpyxl) para obtener bytes.
    """
    resultado: dict[int, bytes] = {}
    with zipfile.ZipFile(xlsx_path) as z:
        nombres = set(z.namelist())
        if "xl/drawings/drawing1.xml" not in nombres:
            return {}
        if "xl/drawings/_rels/drawing1.xml.rels" not in nombres:
            return {}

        rels_xml = z.read("xl/drawings/_rels/drawing1.xml.rels").decode("utf-8")
        ns_rels = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
        rid_a_target = {}
        for rel in ET.fromstring(rels_xml).findall("r:Relationship", ns_rels):
            rid_a_target[rel.get("Id")] = rel.get("Target")

        drawing_xml = z.read("xl/drawings/drawing1.xml").decode("utf-8")
        ns = {
            "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }
        root = ET.fromstring(drawing_xml)
        anchors = []
        for tag in ("oneCellAnchor", "twoCellAnchor"):
            anchors.extend(root.findall(f"xdr:{tag}", ns))

        for a in anchors:
            from_node = a.find("xdr:from", ns)
            pic = a.find("xdr:pic", ns)
            if from_node is None or pic is None:
                continue
            col = int(from_node.find("xdr:col", ns).text)
            if col != 0:  # solo columna A
                continue
            fila = int(from_node.find("xdr:row", ns).text) + 1
            blip = pic.find("xdr:blipFill/a:blip", ns)
            rid = blip.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
            )
            target = rid_a_target.get(rid, "")
            if target.startswith("/"):
                ruta_zip = target.lstrip("/")
            else:
                import os
                ruta_zip = os.path.normpath(
                    os.path.join("xl/drawings", target)
                ).replace("\\", "/")
            if ruta_zip in nombres:
                resultado[fila] = z.read(ruta_zip)
    return resultado


def ingestar_xlsx_intermedio(
    session: Session,
    xlsx_path: str,
    nombre_proveedor: str,
    fotos_destino: str,
) -> int:
    """
    Lee un xlsx intermedio (el output de pdf_a_formato_hd.py paso 3) y lo
    inserta/actualiza en la BD.

    Devuelve el numero de productos NUEVOS insertados (no incluye actualizados).
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active

    # Buscar o crear proveedor
    prov = session.query(Proveedor).filter_by(nombre=nombre_proveedor).first()
    if prov is None:
        prov = Proveedor(
            nombre=nombre_proveedor,
            archivo_pdf=Path(xlsx_path).name,
        )
        session.add(prov)
        session.flush()

    # Extraer imagenes una vez (eficiente)
    imagenes = _extraer_imagenes_xlsx(xlsx_path)

    # Preparar destino de fotos
    fotos_dir = Path(fotos_destino)
    fotos_dir.mkdir(parents=True, exist_ok=True)

    nuevos = 0
    for fila in range(2, ws.max_row + 1):
        sku = ws.cell(fila, COL_SKU).value or ""
        sku = str(sku).strip()
        desc = ws.cell(fila, COL_DESC).value or ""
        desc = str(desc).strip()
        if not (sku or desc):
            continue

        # Buscar producto existente por (proveedor, sku)
        prod = None
        if sku:
            prod = session.query(Producto).filter_by(
                proveedor_id=prov.id, sku=sku
            ).first()

        es_nuevo = prod is None
        if es_nuevo:
            prod = Producto(proveedor_id=prov.id, sku=sku, descripcion=desc)
            session.add(prod)

        # Actualizar campos (siempre, para que ingest sea idempotente)
        prod.descripcion = desc
        prod.fob_usd = _to_float(ws.cell(fila, COL_FOB).value)
        prod.medidas = str(ws.cell(fila, COL_MEDIDAS).value or "").strip()
        prod.material = str(ws.cell(fila, COL_MATERIAL).value or "").strip()
        prod.peso_kg = _to_float(ws.cell(fila, COL_PESO).value)
        prod.color = str(ws.cell(fila, COL_COLOR).value or "").strip()
        prod.moq = str(ws.cell(fila, COL_MOQ).value or "").strip()
        prod.packing = str(ws.cell(fila, COL_PACKING).value or "").strip()
        prod.carton_dims = str(ws.cell(fila, COL_CARTON).value or "").strip()
        prod.cbm = _to_float(ws.cell(fila, COL_CBM).value)
        prod.pzas_20ft = _to_int(ws.cell(fila, COL_PZAS20).value)
        prod.pzas_40hq = _to_int(ws.cell(fila, COL_PZAS40).value)
        prod.lead_time = str(ws.cell(fila, COL_LEAD).value or "").strip()

        session.flush()

        # Foto: copiar bytes a destino y enlazar
        if fila in imagenes and es_nuevo:
            # Determinar extension probable
            data = imagenes[fila]
            ext = ".png"
            if data[:3] == b"\xff\xd8\xff":
                ext = ".jpg"
            nombre_archivo = f"{prov.id}_{prod.id}_{sku or fila}{ext}"
            nombre_archivo = nombre_archivo.replace("/", "_").replace("\\", "_")
            destino = fotos_dir / nombre_archivo
            destino.write_bytes(data)

            foto = Foto(
                producto_id=prod.id,
                ruta_relativa=f"fotos/{nombre_archivo}",
                es_principal=True,
            )
            session.add(foto)

        if es_nuevo:
            nuevos += 1

    return nuevos
```

**Step 5: Correr tests**

Run: `pytest tests/test_ingest.py -v`
Expected: 3 PASS.

**Step 6: Commit**

```bash
git add app/ingest.py tests/test_ingest.py tests/fixtures/
git commit -m "feat(ingest): importar productos desde xlsx intermedio con idempotencia"
```

---

## Task 5: CLI para ingestar todos los intermedios

**Files:**
- Create: `app/cli.py`

**Step 1: Implementar CLI sin tests (es orchestracion simple)**

```python
"""
CLI para ingestar todos los _intermedio_*.xlsx que esten en la raiz del proyecto
a la BD. Tambien permite ingestar uno solo por nombre.

Uso:
    python -m app.cli init                  # crear BD vacia
    python -m app.cli ingestar              # todos los _intermedio_*.xlsx
    python -m app.cli ingestar archivo.xlsx # uno especifico
    python -m app.cli stats                 # contar productos/proveedores
"""

import sys
from pathlib import Path

from app.db import get_session_factory, init_db, DB_PATH
from app.ingest import ingestar_xlsx_intermedio
from app.modelos import Proveedor, Producto


PROYECTO_ROOT = Path(__file__).parent.parent
FOTOS_DIR = PROYECTO_ROOT / "data" / "fotos"


def cmd_init():
    init_db()
    print(f"BD inicializada en: {DB_PATH}")


def cmd_ingestar(patron: str | None = None):
    if not DB_PATH.exists():
        init_db()

    SessionFactory = get_session_factory()
    s = SessionFactory()

    if patron:
        archivos = [PROYECTO_ROOT / patron]
    else:
        archivos = sorted(PROYECTO_ROOT.glob("_intermedio_*.xlsx"))

    if not archivos:
        print("No hay _intermedio_*.xlsx para procesar.")
        return

    total_nuevos = 0
    for xlsx in archivos:
        if not xlsx.exists():
            print(f"  No existe: {xlsx}")
            continue
        # nombre proveedor = del archivo, removiendo _intermedio_ y extension
        nombre = xlsx.stem.replace("_intermedio_", "").replace("_", " ")[:60]
        try:
            n = ingestar_xlsx_intermedio(
                session=s,
                xlsx_path=str(xlsx),
                nombre_proveedor=nombre,
                fotos_destino=str(FOTOS_DIR),
            )
            s.commit()
            print(f"  {xlsx.name}: +{n} productos nuevos")
            total_nuevos += n
        except Exception as e:
            s.rollback()
            print(f"  ERROR {xlsx.name}: {e}")

    print(f"\nTotal productos nuevos: {total_nuevos}")
    s.close()


def cmd_stats():
    SessionFactory = get_session_factory()
    s = SessionFactory()
    np = s.query(Proveedor).count()
    nprod = s.query(Producto).count()
    nmarc = s.query(Producto).filter_by(marcado_cotizar=True).count()
    print(f"Proveedores: {np}")
    print(f"Productos: {nprod}")
    print(f"Marcados para cotizar: {nmarc}")
    s.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "init":
        cmd_init()
    elif cmd == "ingestar":
        patron = sys.argv[2] if len(sys.argv) >= 3 else None
        cmd_ingestar(patron)
    elif cmd == "stats":
        cmd_stats()
    else:
        print(f"Comando desconocido: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
```

**Step 2: Probar manualmente**

Run:
```bash
python -m app.cli init
python -m app.cli ingestar
python -m app.cli stats
```

Expected:
- Crea `data/productos.db`
- Ingesta todos los `_intermedio_*.xlsx` de la raiz (al menos PETLEAD, Xinding, HMTPC, etc.)
- Stats muestra >100 productos.

**Step 3: Commit**

```bash
git add app/cli.py
git commit -m "feat(cli): comando para inicializar BD e ingestar intermedios"
```

---

## Task 6: API FastAPI con endpoints de productos (TDD)

**Files:**
- Create: `tests/test_api.py`
- Create: `app/main.py`
- Create: `app/routes.py`

**Step 1: Tests primero**

```python
import pytest
from fastapi.testclient import TestClient

from app.main import crear_app
from app.modelos import Proveedor, Producto


@pytest.fixture
def cliente(db_session, monkeypatch):
    """TestClient con BD aislada."""
    from app import db as db_mod
    monkeypatch.setattr(db_mod, "get_session_factory", lambda: lambda: db_session)
    app = crear_app()
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
```

**Step 2: Correr tests (deben fallar)**

Run: `pytest tests/test_api.py -v`
Expected: FAIL con `ModuleNotFoundError: app.main`

**Step 3: Implementar `app/routes.py`**

```python
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_session_factory
from app.modelos import Producto, Proveedor
from app.pricing import calcular_landed_mxn, calcular_retail_mxn

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_db():
    SessionFactory = get_session_factory()
    db = SessionFactory()
    try:
        yield db
    finally:
        db.close()


SesionDep = Annotated[Session, Depends(get_db)]


class MarcarBody(BaseModel):
    marcado: bool


class ActualizarBody(BaseModel):
    fob_usd: float | None = None
    descripcion: str | None = None
    notas: str | None = None
    marcado_cotizar: bool | None = None


@router.get("/api/productos")
def listar_productos(
    db: SesionDep,
    marcados: bool | None = Query(None),
    proveedor_id: int | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(200, le=1000),
):
    query = db.query(Producto)
    if marcados is not None:
        query = query.filter(Producto.marcado_cotizar == marcados)
    if proveedor_id is not None:
        query = query.filter(Producto.proveedor_id == proveedor_id)
    if q:
        ql = f"%{q}%"
        query = query.filter(
            (Producto.descripcion.ilike(ql)) | (Producto.sku.ilike(ql))
        )
    total = query.count()
    items = query.limit(limit).all()

    return {
        "total": total,
        "items": [
            {
                "id": p.id,
                "sku": p.sku,
                "descripcion": p.descripcion,
                "fob_usd": p.fob_usd,
                "material": p.material,
                "medidas": p.medidas,
                "moq": p.moq,
                "cbm": p.cbm,
                "marcado_cotizar": p.marcado_cotizar,
                "proveedor": p.proveedor.nombre if p.proveedor else None,
                "fotos": [f.ruta_relativa for f in p.fotos],
            }
            for p in items
        ],
    }


@router.post("/api/productos/{producto_id}/marcar")
def marcar(producto_id: int, body: MarcarBody, db: SesionDep):
    p = db.query(Producto).get(producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")
    p.marcado_cotizar = body.marcado
    db.commit()
    return {"ok": True, "marcado": p.marcado_cotizar}


@router.patch("/api/productos/{producto_id}")
def actualizar(producto_id: int, body: ActualizarBody, db: SesionDep):
    p = db.query(Producto).get(producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")
    if body.fob_usd is not None:
        p.fob_usd = body.fob_usd
    if body.descripcion is not None:
        p.descripcion = body.descripcion
    if body.notas is not None:
        p.notas = body.notas
    if body.marcado_cotizar is not None:
        p.marcado_cotizar = body.marcado_cotizar
    db.commit()
    return {"ok": True}


@router.get("/", response_class=HTMLResponse)
def home(request, db: SesionDep):
    productos = db.query(Producto).limit(500).all()
    return TEMPLATES.TemplateResponse(
        "productos.html",
        {"request": request, "productos": productos},
    )
```

**Step 4: Implementar `app/main.py`**

```python
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.routes import router


def crear_app() -> FastAPI:
    init_db()
    app = FastAPI(title="Mascotas BD")
    app.include_router(router)

    # Servir fotos como archivos estaticos
    fotos_dir = Path(__file__).parent.parent / "data" / "fotos"
    fotos_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/fotos", StaticFiles(directory=str(fotos_dir)), name="fotos")

    return app


app = crear_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080)
```

**Step 5: Correr tests**

Run: `pytest tests/test_api.py -v`
Expected: 4 PASS.

**Step 6: Commit**

```bash
git add app/routes.py app/main.py tests/test_api.py
git commit -m "feat(api): endpoints CRUD productos con tests"
```

---

## Task 7: UI HTML con HTMX

**Files:**
- Create: `app/templates/base.html`
- Create: `app/templates/productos.html`
- Create: `app/templates/_fila.html`

**Step 1: `app/templates/base.html`**

```html
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{% block title %}Mascotas BD{% endblock %}</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://unpkg.com/htmx.org@1.9.10"></script>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
</head>
<body class="bg-gray-50 text-gray-900">
<header class="bg-white shadow px-6 py-4 flex justify-between items-center">
  <h1 class="text-xl font-bold">Productos Mascotas</h1>
  <nav class="text-sm">
    <a href="/" class="hover:underline">Inicio</a>
    <a href="/exportar" class="hover:underline ml-4">Exportar marcados</a>
  </nav>
</header>
<main class="p-6">
  {% block content %}{% endblock %}
</main>
</body>
</html>
```

**Step 2: `app/templates/productos.html`**

Una tabla con: foto, SKU, descripcion, FOB, landed calculado, retail calculado, margen %, marcar checkbox, editar boton. Filtros: por proveedor, busqueda texto, solo marcados.

```html
{% extends "base.html" %}
{% block content %}
<div x-data="{ tc: 20.0, factor: 1.4, margen: 0.40 }">
  <div class="mb-4 flex gap-4 items-center bg-white p-4 rounded shadow text-sm">
    <label>Tipo cambio MXN:
      <input type="number" step="0.1" x-model.number="tc" class="border px-2 py-1 w-20 ml-1">
    </label>
    <label>Factor importacion:
      <input type="number" step="0.05" x-model.number="factor" class="border px-2 py-1 w-20 ml-1">
    </label>
    <label>Margen objetivo:
      <input type="number" step="0.05" x-model.number="margen" class="border px-2 py-1 w-20 ml-1">
    </label>
    <input type="text" placeholder="Buscar..." class="border px-3 py-1 rounded flex-1"
           hx-get="/api/productos" hx-trigger="keyup changed delay:300ms"
           hx-target="#tbody" name="q">
    <a href="/exportar" class="bg-green-600 text-white px-4 py-1 rounded">Exportar marcados</a>
  </div>

  <table class="bg-white w-full text-sm shadow rounded overflow-hidden">
    <thead class="bg-gray-100 text-left">
      <tr>
        <th class="p-2">Foto</th>
        <th class="p-2">SKU</th>
        <th class="p-2">Descripcion</th>
        <th class="p-2">FOB USD</th>
        <th class="p-2">Landed MXN</th>
        <th class="p-2">Retail MXN</th>
        <th class="p-2">Margen %</th>
        <th class="p-2">Cotizar</th>
      </tr>
    </thead>
    <tbody id="tbody">
      {% for p in productos %}
        {% include "_fila.html" %}
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
```

**Step 3: `app/templates/_fila.html`**

```html
<tr class="border-t hover:bg-gray-50" x-data="{ marc: {{ 'true' if p.marcado_cotizar else 'false' }} }">
  <td class="p-2">
    {% if p.fotos %}
      <img src="/{{ p.fotos[0].ruta_relativa }}" class="w-16 h-16 object-contain">
    {% endif %}
  </td>
  <td class="p-2 font-mono">{{ p.sku or '' }}</td>
  <td class="p-2 max-w-md">{{ p.descripcion }}</td>
  <td class="p-2">{{ "%.2f"|format(p.fob_usd) if p.fob_usd else '' }}</td>
  <td class="p-2" x-text="(({{ p.fob_usd or 0 }}) * tc * factor).toFixed(2)"></td>
  <td class="p-2" x-text="((({{ p.fob_usd or 0 }}) * tc * factor / (1 - margen)) * 1.16).toFixed(0)"></td>
  <td class="p-2" x-text="(margen * 100).toFixed(0) + '%'"></td>
  <td class="p-2 text-center">
    <input type="checkbox" x-model="marc"
           @change="fetch('/api/productos/{{ p.id }}/marcar', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({marcado: marc})})">
  </td>
</tr>
```

**Step 4: Probar manualmente**

Run:
```bash
python -m app.main
```

Abrir http://127.0.0.1:8080 — debe mostrar la tabla con productos ingeridos, sliders para tipo cambio/factor/margen que recalculan precios en tiempo real, checkboxes para marcar.

**Step 5: Commit**

```bash
git add app/templates/
git commit -m "feat(ui): pagina principal con tabla, calculo en vivo y marcar"
```

---

## Task 8: Exportar productos marcados al formato HD (TDD)

**Files:**
- Create: `tests/test_exportar.py`
- Create: `app/exportar.py`

**Step 1: Test**

```python
from pathlib import Path
from app.exportar import generar_formato_hd_desde_marcados
from app.modelos import Proveedor, Producto, Foto


def test_exportar_marcados(db_session, tmp_path):
    prov = Proveedor(nombre="V", archivo_pdf="v.pdf")
    db_session.add(prov)
    db_session.commit()

    p1 = Producto(
        proveedor_id=prov.id, sku="A1", descripcion="Desc A1",
        fob_usd=10.0, marcado_cotizar=True,
    )
    p2 = Producto(
        proveedor_id=prov.id, sku="A2", descripcion="Desc A2",
        fob_usd=20.0, marcado_cotizar=False,  # no marcado
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    # Foto fake
    foto_dir = tmp_path / "fotos"
    foto_dir.mkdir()
    png = foto_dir / "p1.png"
    png.write_bytes(bytes.fromhex(
        "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4"
        "890000000D49444154789C636400000000050001A5F645620000000049454E44AE426082"
    ))
    db_session.add(Foto(producto_id=p1.id, ruta_relativa="fotos/p1.png", es_principal=True))
    db_session.commit()

    out = tmp_path / "_intermedio_marcados.xlsx"
    n = generar_formato_hd_desde_marcados(
        session=db_session,
        xlsx_intermedio=str(out),
        base_fotos=str(tmp_path),
    )
    assert n == 1  # solo p1 estaba marcado
    assert out.exists()

    # Verificar el xlsx
    import openpyxl
    wb = openpyxl.load_workbook(str(out))
    ws = wb.active
    assert ws.cell(2, 2).value == "A1"  # SKU en col B
    assert ws.cell(2, 15).value == 10.0  # FOB en col O
    assert len(ws._images) == 1
```

**Step 2: Correr (debe fallar)**

Run: `pytest tests/test_exportar.py -v`
Expected: FAIL.

**Step 3: Implementar `app/exportar.py`**

```python
"""
Genera el xlsx intermedio a partir de los productos marcados en la BD.
Despues, ese xlsx se puede pasar a llenar_formato_hd.py para producir
el formato HD final.
"""

from pathlib import Path

import openpyxl
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font

from sqlalchemy.orm import Session

from app.modelos import Producto


def generar_formato_hd_desde_marcados(
    session: Session,
    xlsx_intermedio: str,
    base_fotos: str,
) -> int:
    """
    Construye un xlsx intermedio (mismo layout que pdf_a_formato_hd.py)
    con todos los productos marcados_cotizar=True.

    Devuelve la cantidad de productos exportados.
    """
    productos = (
        session.query(Producto)
        .filter(Producto.marcado_cotizar.is_(True))
        .all()
    )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cotizacion seleccionados"

    headers = ["Foto", "SKU", "Descripcion", "Medidas", "Material", "Peso (kg)",
               "Color", "MOQ", "Packing", "Carton dims", "CBM",
               "Pzas 20ft", "Pzas 40hq", "Lead time", "FOB USD"]
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = Font(bold=True)
    ws.row_dimensions[1].height = 25

    for i, p in enumerate(productos, start=2):
        ws.row_dimensions[i].height = 90
        ws.cell(i, 2, value=p.sku or "")
        ws.cell(i, 3, value=p.descripcion or "")
        ws.cell(i, 4, value=p.medidas or "")
        ws.cell(i, 5, value=p.material or "")
        ws.cell(i, 6, value=p.peso_kg)
        ws.cell(i, 7, value=p.color or "")
        ws.cell(i, 8, value=p.moq or "")
        ws.cell(i, 9, value=p.packing or "")
        ws.cell(i, 10, value=p.carton_dims or "")
        ws.cell(i, 11, value=p.cbm)
        ws.cell(i, 12, value=p.pzas_20ft)
        ws.cell(i, 13, value=p.pzas_40hq)
        ws.cell(i, 14, value=p.lead_time or "")
        ws.cell(i, 15, value=p.fob_usd)

        # Foto principal
        if p.fotos:
            foto_path = Path(base_fotos) / p.fotos[0].ruta_relativa
            if foto_path.exists():
                try:
                    img = XLImage(str(foto_path))
                    img.width = min(img.width, 120)
                    img.height = min(img.height, 120)
                    img.anchor = f"A{i}"
                    ws.add_image(img)
                except Exception:
                    pass

    Path(xlsx_intermedio).parent.mkdir(parents=True, exist_ok=True)
    wb.save(xlsx_intermedio)
    return len(productos)
```

**Step 4: Correr tests**

Run: `pytest tests/test_exportar.py -v`
Expected: PASS.

**Step 5: Agregar endpoint `/exportar` en routes**

Anadir a `app/routes.py`:

```python
from fastapi.responses import FileResponse
import subprocess
import sys


@router.get("/exportar")
def exportar(db: SesionDep):
    """Genera xlsx intermedio + ejecuta llenar_formato_hd.py."""
    from app.exportar import generar_formato_hd_desde_marcados

    proyecto = Path(__file__).parent.parent
    xlsx_int = proyecto / "_intermedio_seleccion.xlsx"
    n = generar_formato_hd_desde_marcados(
        session=db,
        xlsx_intermedio=str(xlsx_int),
        base_fotos=str(proyecto / "data"),
    )
    if n == 0:
        raise HTTPException(400, "No hay productos marcados.")

    formato = proyecto / "Formato HD-Mascotas.xlsb"
    script = proyecto / "llenar_formato_hd.py"
    result = subprocess.run(
        [sys.executable, str(script), str(xlsx_int), str(formato), "--mapeo", "C=8,O=11"],
        capture_output=True, text=True, cwd=str(proyecto),
    )
    if result.returncode != 0:
        raise HTTPException(500, f"Fallo llenar_formato_hd: {result.stderr[:500]}")

    salida = proyecto / "formato-hd-_intermedio_seleccion.xlsx"
    if not salida.exists():
        # llenar_formato_hd lo nombra removiendo _intermedio_
        salida = proyecto / "formato-hd-seleccion.xlsx"
    if not salida.exists():
        raise HTTPException(500, f"No encontre el archivo de salida")

    return FileResponse(str(salida), filename=salida.name)
```

**Step 6: Probar manualmente**

1. Arrancar server: `python -m app.main`
2. En UI, marcar 3-5 productos.
3. Click "Exportar marcados" → descarga el `formato-hd-XXX.xlsx`.

**Step 7: Commit**

```bash
git add app/exportar.py app/routes.py tests/test_exportar.py
git commit -m "feat(exportar): generar formato HD desde productos marcados"
```

---

## Task 9: Ingest directo desde PDF (opcional, integra con pdf_a_formato_hd.py)

**Files:**
- Modify: `app/cli.py` para anadir comando `pdf <archivo.pdf>`

**Step 1: Anadir funcion al CLI**

```python
def cmd_pdf(pdf_path: str):
    """Procesa un PDF nuevo y lo ingesta a la BD."""
    import subprocess

    proyecto = PROYECTO_ROOT
    script = proyecto / "pdf_a_formato_hd.py"

    # Correr pdf_a_formato_hd para extraer y crear _intermedio_*
    result = subprocess.run(
        [sys.executable, str(script), pdf_path],
        cwd=str(proyecto),
    )
    if result.returncode != 0:
        print("Fallo pdf_a_formato_hd")
        return

    # Buscar el _intermedio_*.xlsx mas reciente
    intermedios = sorted(
        proyecto.glob("_intermedio_*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not intermedios:
        print("No se genero _intermedio_*.xlsx")
        return

    cmd_ingestar(intermedios[0].name)
```

Anadir en `main()`:
```python
    elif cmd == "pdf":
        if len(sys.argv) < 3:
            print("Uso: python -m app.cli pdf <archivo.pdf>")
            return
        cmd_pdf(sys.argv[2])
```

**Step 2: Probar con un PDF**

Run:
```bash
python -m app.cli pdf "2026-04-23__Fw Zhejiang Xinding Plastic Quotation Sh__Zhejiang Xinding Plastic Quotation Sheet 20260422.pdf"
python -m app.cli stats
```

**Step 3: Commit**

```bash
git add app/cli.py
git commit -m "feat(cli): comando pdf para procesar e ingestar de un solo paso"
```

---

## Task 10: README y documentacion final

**Files:**
- Create: `README.md`

**Step 1: Documentar uso**

```markdown
# Base de Datos de Productos Mascotas

## Instalacion

```bash
pip install -e ".[dev]"
python -m app.cli init
```

## Flujo tipico

1. Procesar PDFs nuevos:
   ```bash
   python -m app.cli pdf "cotizacion.pdf"
   ```
   Esto ejecuta el pipeline (Adobe + Haiku si hace falta) y mete los productos en la BD.

2. O ingestar todos los intermedios existentes:
   ```bash
   python -m app.cli ingestar
   ```

3. Arrancar UI:
   ```bash
   python -m app.main
   ```
   Abrir http://127.0.0.1:8080.

4. En la UI:
   - Ajustar tipo de cambio, factor importacion, margen objetivo.
   - Buscar productos por SKU o descripcion.
   - Marcar candidatos a cotizar.
   - Click "Exportar marcados" para descargar el formato HD final.

## Estructura

- `data/productos.db` -- SQLite con productos, proveedores, fotos.
- `data/fotos/` -- imagenes copiadas.
- `_intermedio_*.xlsx` -- output del pipeline PDF, son ingeridos.
- `formato-hd-*.xlsx` -- formato HD final para enviar a Home Depot.

## Tests

```bash
pytest -v
```
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README con flujo de uso"
```

---

## Plan complete and saved.

### Resumen del plan

10 tasks que cubren:

1. **Setup** -- estructura del proyecto, pyproject.toml, fixture de BD para tests.
2. **Modelos** -- `Proveedor`, `Producto`, `Foto` con tests de integridad referencial.
3. **Pricing** -- funciones puras `landed_mxn`, `retail_mxn`, `margen_hd`.
4. **Ingest xlsx** -- importar de `_intermedio_*.xlsx` con idempotencia (re-ingest no duplica).
5. **CLI** -- `init`, `ingestar`, `stats`.
6. **API** -- FastAPI con `GET /api/productos`, `POST .../marcar`, `PATCH .../actualizar`.
7. **UI** -- HTML + HTMX + Alpine + Tailwind: tabla con calculo en vivo, filtros, marcar.
8. **Exportar** -- generar formato HD desde productos marcados (reusa `llenar_formato_hd.py`).
9. **CLI PDF** -- procesar e ingestar de un solo paso.
10. **Documentacion**.

### Decisiones clave

- **SQLite local** en `data/productos.db` -- portable, sin servidor.
- **Idempotencia** -- re-ingerir el mismo xlsx actualiza pero no duplica.
- **Marcado de cotizar preservado** -- al actualizar, NO se sobrescribe el flag (proteccion contra perder seleccion).
- **Reuso del codigo existente** -- el pipeline `pdf_a_formato_hd.py`, `llenar_formato_hd.py` y `extraer_con_claude.py` siguen siendo la unica fuente de extraccion; la app solo orquesta y persiste.
- **Calculo de precios en cliente** (Alpine.js) -- no se persiste el calculo, solo el FOB. Asi puedes cambiar tipo de cambio sin tocar la BD.
- **Fotos como archivos** en `data/fotos/`, no blobs -- mas facil de inspeccionar y backupear.

### Costos esperados durante implementacion

Sin gastos Adobe/Anthropic adicionales -- todo lo nuevo es Python local. Solo cuando ingestes PDFs nuevos.

### Sobre las 2 imagenes que viste mal extraidas

En el plan no las arreglo automaticamente -- los datos quedan en la BD y desde la UI puedes editar la descripcion y/o re-asignar foto manualmente. Si quieres un workflow de "re-asignar foto" en la UI, dimelo y lo agrego como Task 11.

---

Plan complete and saved to `docs/plans/2026-05-10-base-datos-productos-mascotas.md`. Two execution options:

**1. Subagent-Driven (this session)** — I dispatch fresh subagent per task, review between tasks, fast iteration.

**2. Parallel Session (separate)** — Open new session with executing-plans, batch execution with checkpoints. Sugerido si quieres limpiar contexto.

Como mencionaste GSD y querer limpiar el contexto, te recomiendo **opcion 2**: abre una nueva sesion en este mismo directorio y pidele que ejecute el plan con la skill `superpowers:executing-plans` o `/gsd-execute-phase`. El plan es 100% autocontenido — el nuevo agente no necesita conversacion previa.

¿Cual prefieres?
