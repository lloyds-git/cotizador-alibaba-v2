from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import db as db_module
from app.modelos import Producto

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SIN_CATEGORIA = "__sin_categoria__"  # sentinela en query string


def get_db():
    # Lookup tardio para permitir monkeypatch en tests
    SessionFactory = db_module.get_session_factory()
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
    categoria: str | None = None
    subcategoria: str | None = None


class MarcarBulkBody(BaseModel):
    ids: list[int]
    marcado: bool


def _aplicar_filtro_categoria(query, categoria: str | None):
    """Aplica filtro de categoria, soportando el sentinela 'sin_categoria'."""
    if categoria is None:
        return query
    if categoria == SIN_CATEGORIA:
        return query.filter(Producto.categoria.is_(None))
    return query.filter(Producto.categoria == categoria)


@router.get("/api/productos")
def listar_productos(
    db: SesionDep,
    marcados: bool | None = Query(None),
    proveedor_id: int | None = Query(None),
    categoria: str | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(200, le=1000),
):
    query = db.query(Producto)
    if marcados is not None:
        query = query.filter(Producto.marcado_cotizar == marcados)
    if proveedor_id is not None:
        query = query.filter(Producto.proveedor_id == proveedor_id)
    query = _aplicar_filtro_categoria(query, categoria)
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
                "categoria": p.categoria,
                "subcategoria": p.subcategoria,
                "marcado_cotizar": p.marcado_cotizar,
                "proveedor": p.proveedor.nombre if p.proveedor else None,
                "fotos": [f.ruta_relativa for f in p.fotos],
            }
            for p in items
        ],
    }


@router.get("/api/categorias")
def listar_categorias(db: SesionDep):
    """Devuelve categorias con total y cuantos marcados, ordenadas por total desc."""
    filas = (
        db.query(
            Producto.categoria,
            func.count(Producto.id).label("total"),
            func.sum(
                func.coalesce(Producto.marcado_cotizar, 0)
            ).label("marcados"),
        )
        .group_by(Producto.categoria)
        .all()
    )
    out = []
    for cat, total, marcados in filas:
        out.append(
            {
                "categoria": cat,
                "total": int(total),
                "marcados": int(marcados or 0),
            }
        )
    # ordenar por total desc, dejando None al final
    out.sort(key=lambda r: (r["categoria"] is None, -r["total"]))
    return {"items": out}


@router.post("/api/productos/{producto_id}/marcar")
def marcar(producto_id: int, body: MarcarBody, db: SesionDep):
    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")
    p.marcado_cotizar = body.marcado
    db.commit()
    return {"ok": True, "marcado": p.marcado_cotizar}


@router.post("/api/productos/marcar-bulk")
def marcar_bulk(body: MarcarBulkBody, db: SesionDep):
    """Marca o desmarca varios productos a la vez."""
    if not body.ids:
        return {"ok": True, "afectados": 0}
    n = (
        db.query(Producto)
        .filter(Producto.id.in_(body.ids))
        .update({Producto.marcado_cotizar: body.marcado}, synchronize_session=False)
    )
    db.commit()
    return {"ok": True, "afectados": n}


@router.get("/api/productos/{producto_id}/cotizar")
def cotizar_14_pasos(
    producto_id: int,
    db: SesionDep,
    tc: float | None = Query(None, description="Tipo de cambio MXN/USD override"),
    margen_nuestro: float | None = Query(None, description="Margen Lloyds (0-1)"),
    margen_cliente: float | None = Query(None, description="Margen retailer (0-1)"),
    flete_maritimo_usd: float | None = Query(None, description="Flete maritimo USD/contenedor"),
    piezas: int | None = Query(None, description="Piezas/40HQ override"),
):
    """Devuelve los 14 pasos del motor de cotizacion para un producto."""
    from app.cotizador.adapter import producto_a_row
    from app.cotizador.engine import compute_for_row, STEP_LABELS

    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")

    row = producto_a_row(p)
    res = compute_for_row(
        row,
        override_tc=tc,
        override_piezas_contenedor=piezas,
        override_flete_maritimo_usd=flete_maritimo_usd,
        margen_nuestro_pct=margen_nuestro,
        margen_cliente_pct=margen_cliente,
    )

    return {
        "producto_id": producto_id,
        "sku": p.sku,
        "descripcion": p.descripcion,
        "categoria": p.categoria,
        "subcategoria": p.subcategoria,
        "fob_usd": p.fob_usd,
        "material": p.material,
        "medidas": p.medidas,
        "peso_kg": p.peso_kg,
        "color": p.color,
        "moq": p.moq,
        "packing": p.packing,
        "carton_dims": p.carton_dims,
        "cbm": p.cbm,
        "pzas_20ft": p.pzas_20ft,
        "pzas_40hq": p.pzas_40hq,
        "lead_time": p.lead_time,
        "proveedor": p.proveedor.nombre if p.proveedor else None,
        "fotos": [f.ruta_relativa for f in p.fotos],
        "fraccion_arancelaria": res.fraccion_arancelaria,
        "tasa_arancelaria_pct": str(res.tasa_arancelaria_pct),
        "tipo_cambio": str(res.tipo_cambio),
        "margen_nuestro": str(res.margen_nuestro_effective),
        "margen_cliente": str(res.margen_cliente_effective),
        "pasos": [
            {
                "n": i,
                "label": STEP_LABELS[i - 1],
                "valor": str(getattr(res, f"paso{i}")),
            }
            for i in range(1, 15)
        ],
        "warnings": res.warnings,
    }


@router.patch("/api/productos/{producto_id}")
def actualizar(producto_id: int, body: ActualizarBody, db: SesionDep):
    p = db.get(Producto, producto_id)
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
    if body.categoria is not None:
        # Cadena vacia significa "limpiar"
        p.categoria = body.categoria.strip() or None
    if body.subcategoria is not None:
        p.subcategoria = body.subcategoria.strip() or None
    db.commit()
    return {"ok": True}


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: SesionDep):
    productos = db.query(Producto).limit(500).all()
    # Categorias para el dropdown de filtro
    cats = (
        db.query(Producto.categoria, func.count(Producto.id))
        .group_by(Producto.categoria)
        .all()
    )
    categorias = sorted(
        [
            {"categoria": c, "total": int(n)}
            for c, n in cats
        ],
        key=lambda r: (r["categoria"] is None, -r["total"]),
    )
    return TEMPLATES.TemplateResponse(
        "productos.html",
        {
            "request": request,
            "productos": productos,
            "categorias": categorias,
        },
    )


def _correr_llenar_formato_hd(
    db: Session,
    xlsx_int: Path,
    salida: Path,
    categoria: str | None = "__usar_marcados__",
) -> Path:
    """Genera intermedio (por marcas o por categoria) + corre llenar_formato_hd.py.

    Si categoria == '__usar_marcados__' (default): filtra por marcado_cotizar=True.
    Si categoria es None: filtra productos sin categoria.
    Si categoria es str: filtra por esa categoria.

    Devuelve la ruta del archivo HD producido. Lanza HTTPException en error.
    """
    import subprocess
    import sys as _sys
    from app.exportar import (
        generar_formato_hd_desde_marcados,
        generar_formato_hd_por_categoria,
    )

    proyecto = Path(__file__).parent.parent
    if categoria == "__usar_marcados__":
        n = generar_formato_hd_desde_marcados(
            session=db,
            xlsx_intermedio=str(xlsx_int),
            base_fotos=str(proyecto / "data"),
        )
        if n == 0:
            raise HTTPException(400, "No hay productos marcados.")
    else:
        n = generar_formato_hd_por_categoria(
            session=db,
            xlsx_intermedio=str(xlsx_int),
            base_fotos=str(proyecto / "data"),
            categoria=categoria,
        )
        if n == 0:
            raise HTTPException(404, f"No hay productos en categoria {categoria!r}.")

    formato = proyecto / "Formato HD-Mascotas.xlsb"
    script = proyecto / "llenar_formato_hd.py"

    # Borrar salida previa
    salida_default = proyecto / f"formato-hd-{xlsx_int.stem.lower()}.xlsx"
    for p in {salida, salida_default}:
        if p.exists():
            try:
                p.unlink()
            except PermissionError:
                raise HTTPException(
                    500,
                    f"No puedo borrar el archivo anterior (esta abierto en Excel?): {p.name}",
                )

    # El intermedio de exportar.py tiene 15 columnas (Foto, SKU, Descripcion, ...,
    # CBM=K, Lead time=N, FOB USD=O). El default de llenar_formato_hd.py asume
    # 22 columnas (layout viejo de pdf_a_formato_hd), por eso forzamos el mapeo.
    result = subprocess.run(
        [
            _sys.executable, str(script), str(xlsx_int), str(formato),
            "--mapeo", "C=8,K=11,N=16,O=17",
        ],
        capture_output=True, text=True, cwd=str(proyecto),
    )
    if result.returncode != 0:
        raise HTTPException(500, f"Fallo llenar_formato_hd: {result.stderr[:500]}")

    # llenar_formato_hd.py escribe a salida_default; si nos pidieron otro nombre, renombrar.
    if not salida_default.exists():
        raise HTTPException(500, f"No encontre el archivo de salida: {salida_default.name}")
    if salida_default != salida:
        salida_default.replace(salida)
    return salida


@router.get("/exportar")
def exportar(db: SesionDep):
    """Genera HD desde la seleccion actual (compatibilidad)."""
    proyecto = Path(__file__).parent.parent
    xlsx_int = proyecto / "_intermedio_seleccion.xlsx"
    salida = proyecto / f"formato-hd-{xlsx_int.stem.lower()}.xlsx"
    archivo = _correr_llenar_formato_hd(db, xlsx_int, salida)
    return FileResponse(str(archivo), filename=archivo.name)


@router.get("/exportar/{categoria}")
def exportar_categoria(categoria: str, db: SesionDep):
    """Genera HD para una categoria sin tocar el estado de marcas.

    Si categoria == '__sin_categoria__', exporta productos sin categoria.
    """
    proyecto = Path(__file__).parent.parent

    # Validar que la categoria existe (con productos)
    q = _aplicar_filtro_categoria(db.query(Producto), categoria)
    n_en_cat = q.count()
    if n_en_cat == 0:
        raise HTTPException(404, f"Categoria '{categoria}' no tiene productos.")

    # Nombre con fecha del dia (no del correo origen, como dice el plan)
    cat_slug = "sin-categoria" if categoria == SIN_CATEGORIA else categoria
    fecha = date.today().strftime("%Y%m%d")
    xlsx_int = proyecto / f"_intermedio_{cat_slug}-{fecha}.xlsx"
    salida = proyecto / f"formato-hd-{cat_slug}-{fecha}.xlsx"

    # Pasamos categoria=None si el cliente uso el sentinela __sin_categoria__
    cat_filter = None if categoria == SIN_CATEGORIA else categoria
    archivo = _correr_llenar_formato_hd(db, xlsx_int, salida, categoria=cat_filter)
    return FileResponse(str(archivo), filename=archivo.name)
