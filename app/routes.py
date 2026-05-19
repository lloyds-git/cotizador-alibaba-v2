from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, Integer, case, cast
from sqlalchemy.orm import Session

from app import db as db_module
from app.modelos import (
    Producto, Proveedor, ArancelOverride, Arancel, CostoAdicional,
    CotizacionSnapshot, Foto, Categoria, CategoriaKeyword, PatronDescarte,
    UsuarioAutorizado,
)
from app.clasificador import clasificar_descripcion, invalidar_cache
from app.ingest import (
    cbm_desde_carton_dims,
    cbm_es_discrepante,
    pzas_40hq_desde_cbm_y_caja,
    pzas_caja_desde_nw_y_peso,
)

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
    material: str | None = None
    medidas: str | None = None
    peso_kg: float | None = None
    color: str | None = None
    moq: str | None = None
    packing: str | None = None
    carton_dims: str | None = None
    cbm: float | None = None
    pzas_20ft: int | None = None
    pzas_40hq: int | None = None
    pzas_caja: int | None = None
    nw_caja_kg: float | None = None
    gw_caja_kg: float | None = None
    lead_time: str | None = None
    item_type: str | None = None


class MarcarBulkBody(BaseModel):
    ids: list[int]
    marcado: bool


class BorrarBulkBody(BaseModel):
    ids: list[int]


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


@router.get("/api/proveedores")
def listar_proveedores(db: SesionDep):
    """Devuelve proveedores con conteo de productos. Solo lista los que tienen
    al menos 1 producto (para evitar opciones inutiles en el filtro)."""
    filas = (
        db.query(
            Proveedor.id,
            Proveedor.nombre,
            func.count(Producto.id).label("total"),
        )
        .join(Producto, Producto.proveedor_id == Proveedor.id)
        .group_by(Proveedor.id, Proveedor.nombre)
        .order_by(Proveedor.nombre)
        .all()
    )
    return [
        {"id": pid, "nombre": nombre, "total": int(total)}
        for pid, nombre, total in filas
    ]


@router.get("/api/categorias")
def listar_categorias(db: SesionDep):
    """Devuelve categorias con total y cuantos marcados.

    Fuente: tabla 'categorias' (catalogo formal) + cualquier categoria
    huerfana (presente en productos.categoria pero no en el catalogo) por
    compatibilidad. El orden respeta el campo 'orden' del catalogo;
    las huerfanas van al final.

    Marcadores boolean: CAST a Integer porque SQLAlchemy convierte
    SUM(bool) -> bool (devuelve True en vez del conteo real).
    """
    marcados_expr = func.sum(
        cast(case((Producto.marcado_cotizar.is_(True), 1), else_=0), Integer)
    )

    # Conteos por valor presente en productos.categoria (incluyendo NULL)
    conteos = {
        cat: (int(total), int(marcados or 0))
        for cat, total, marcados in (
            db.query(
                Producto.categoria,
                func.count(Producto.id).label("total"),
                marcados_expr.label("marcados"),
            )
            .group_by(Producto.categoria)
            .all()
        )
    }

    out = []
    slugs_catalogo = set()
    for c in db.query(Categoria).order_by(Categoria.orden.asc(), Categoria.slug.asc()).all():
        slugs_catalogo.add(c.slug)
        total, marcados = conteos.get(c.slug, (0, 0))
        out.append({
            "categoria": c.slug,
            "total": total,
            "marcados": marcados,
            "orden": c.orden,
            "en_catalogo": True,
        })

    # Huerfanas: valores en productos.categoria que no estan en el catalogo
    for cat, (total, marcados) in conteos.items():
        if cat in slugs_catalogo or cat is None:
            continue
        out.append({
            "categoria": cat,
            "total": total,
            "marcados": marcados,
            "orden": 9999,
            "en_catalogo": False,
        })

    # Categoria None (sin clasificar): siempre al final si hay productos asi
    if None in conteos:
        total, marcados = conteos[None]
        out.append({
            "categoria": None,
            "total": total,
            "marcados": marcados,
            "orden": 99999,
            "en_catalogo": False,
        })

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


def _borrar_productos_por_ids(db: Session, ids: list[int]) -> int:
    """Borra productos y sus dependencias (snapshots, costos, fotos) en bulk.

    No usamos cascade ORM en todos los relacionamientos (CostoAdicional y
    CotizacionSnapshot usan backref sin cascade), asi que limpiamos a mano
    en orden inverso de FK antes de borrar el Producto.
    """
    if not ids:
        return 0
    db.query(CotizacionSnapshot).filter(
        CotizacionSnapshot.producto_id.in_(ids)
    ).delete(synchronize_session=False)
    db.query(CostoAdicional).filter(
        CostoAdicional.producto_id.in_(ids)
    ).delete(synchronize_session=False)
    db.query(Foto).filter(Foto.producto_id.in_(ids)).delete(synchronize_session=False)
    n = (
        db.query(Producto)
        .filter(Producto.id.in_(ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return n


@router.post("/api/productos/borrar-bulk")
def borrar_bulk(body: BorrarBulkBody, db: SesionDep):
    """Borra varios productos por id (con sus snapshots/costos/fotos)."""
    n = _borrar_productos_por_ids(db, body.ids)
    return {"ok": True, "borrados": n}


EXTENSIONES_FOTO = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_FOTO_BYTES = 10 * 1024 * 1024  # 10 MB

MAX_PDF_BYTES = 50 * 1024 * 1024  # 50 MB
EXTENSIONES_PDF = {".pdf"}


@router.post("/api/ingest/pdf")
async def ingestar_pdf(
    db: SesionDep,
    file: UploadFile = File(...),
    forzar: bool = Form(False),
):
    """Sube un PDF de cotizacion y lo ingesta a la BD.

    Flujo: guarda el PDF en PDF_INGEST_DIR -> corre pdf_a_formato_hd.py
    -> ingestar_xlsx_intermedio -> gate de calidad.

    Si el gate veredicto es REINGESTAR y forzar=False, devuelve 422 con
    motivo. El cliente puede reintentar con forzar=True.
    """
    from app.pdf_pipeline import procesar_pdf, PdfPipelineError

    nombre_orig = file.filename or ""
    ext = Path(nombre_orig).suffix.lower()
    if ext not in EXTENSIONES_PDF:
        raise HTTPException(
            400,
            f"Extension no permitida: '{ext}'. Solo se acepta .pdf",
        )

    contenido = await file.read()
    if not contenido:
        raise HTTPException(400, "Archivo vacio")
    if len(contenido) > MAX_PDF_BYTES:
        # 413 Payload Too Large
        raise HTTPException(
            413,
            f"Archivo demasiado grande ({len(contenido) // (1024*1024)} MB, max {MAX_PDF_BYTES // (1024*1024)} MB)",
        )

    proyecto = Path(__file__).parent.parent
    ingest_dir = _pdf_ingest_dir()
    ingest_dir.mkdir(parents=True, exist_ok=True)

    # Sanitiza el nombre: solo basename, conserva extension. Si ya existe,
    # agrega sufijo _{epoch} antes de la extension para no pisar.
    base = Path(nombre_orig).name or "upload.pdf"
    destino = ingest_dir / base
    if destino.exists():
        import time
        stem = destino.stem
        destino = ingest_dir / f"{stem}_{int(time.time())}{ext}"
    destino.write_bytes(contenido)

    fotos_destino = proyecto / "data" / "fotos"
    fotos_destino.mkdir(parents=True, exist_ok=True)

    try:
        resultado = procesar_pdf(
            session=db,
            pdf_path=destino,
            fotos_destino=fotos_destino,
            forzar_calidad=forzar,
        )
    except PdfPipelineError as e:
        raise HTTPException(500, str(e))

    if not resultado.get("ok"):
        # Gate REINGESTAR: 422 con motivo y bandera puede_forzar
        return JSONResponse(status_code=422, content=resultado)
    return resultado


@router.post("/api/productos/{producto_id}/foto")
async def subir_foto(producto_id: int, db: SesionDep, file: UploadFile = File(...)):
    """Sube o reemplaza la foto principal del producto.

    Si ya existia una foto principal: borra el archivo del disco y la fila Foto.
    Guarda la nueva en data/fotos/ con nombre derivado del producto + epoch.
    """
    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")

    nombre_orig = file.filename or ""
    ext = Path(nombre_orig).suffix.lower()
    if ext not in EXTENSIONES_FOTO:
        raise HTTPException(
            400,
            f"Extension no permitida: '{ext}'. Permitidas: {sorted(EXTENSIONES_FOTO)}",
        )

    contenido = await file.read()
    if not contenido:
        raise HTTPException(400, "Archivo vacio")
    if len(contenido) > MAX_FOTO_BYTES:
        raise HTTPException(
            400,
            f"Archivo demasiado grande ({len(contenido)} bytes, max {MAX_FOTO_BYTES})",
        )

    proyecto = Path(__file__).parent.parent
    fotos_dir = proyecto / "data" / "fotos"
    fotos_dir.mkdir(parents=True, exist_ok=True)

    # Naming: {producto_id}_user_{epoch}{ext}. El prefijo "_user_" diferencia
    # de las fotos importadas (formato {prov}_{seq}_{sku}) y el epoch evita
    # colision al reemplazar varias veces el mismo producto.
    import time
    nombre_nuevo = f"{producto_id}_user_{int(time.time())}{ext}"
    destino = fotos_dir / nombre_nuevo
    destino.write_bytes(contenido)

    # Borrar foto(s) previa(s): archivo en disco + filas. Si el archivo no
    # existe (ya fue borrado manualmente), no aborta.
    fotos_viejas = db.query(Foto).filter_by(producto_id=producto_id).all()
    for f in fotos_viejas:
        rel = (f.ruta_relativa or "").replace("\\", "/")
        # rel viene como "fotos/<nombre>"; resolvemos contra data/
        if rel.startswith("fotos/"):
            archivo_viejo = proyecto / "data" / rel
            try:
                if archivo_viejo.exists() and archivo_viejo.resolve().is_relative_to(fotos_dir.resolve()):
                    archivo_viejo.unlink()
            except (OSError, ValueError):
                pass
        db.delete(f)

    nueva = Foto(
        producto_id=producto_id,
        ruta_relativa=f"fotos/{nombre_nuevo}",
        es_principal=True,
    )
    db.add(nueva)
    db.commit()
    db.refresh(nueva)
    return {
        "ok": True,
        "id": nueva.id,
        "ruta_relativa": nueva.ruta_relativa,
        "url": f"/{nueva.ruta_relativa}",
    }


@router.delete("/api/proveedores/{proveedor_id}/productos")
def borrar_productos_de_proveedor(proveedor_id: int, db: SesionDep):
    """Borra TODOS los productos de un proveedor (con sus dependencias)."""
    pv = db.get(Proveedor, proveedor_id)
    if not pv:
        raise HTTPException(404, "Proveedor no existe")
    ids = [
        pid for (pid,) in db.query(Producto.id)
        .filter(Producto.proveedor_id == proveedor_id)
        .all()
    ]
    n = _borrar_productos_por_ids(db, ids)
    return {"ok": True, "borrados": n, "proveedor": pv.nombre}


def _pdf_ingest_dir() -> Path:
    """Carpeta de PDFs a procesar (matchea el helper en app/cli.py)."""
    import os
    proyecto = Path(__file__).parent.parent
    nombre = os.environ.get("PDF_INGEST_DIR") or "indigest-pdf"
    p = Path(nombre)
    return p if p.is_absolute() else proyecto / p


def _localizar_origen(p: Producto) -> dict | None:
    """Devuelve {ruta: Path, tipo: '.pdf'|'.xlsx'|..., fuente: str} si encuentra
    el archivo original del producto. Estrategia:
      1. Buscar archivo_pdf directamente en PDF_INGEST_DIR (flujo nuevo).
      2. Si no, caer al manifest legacy (data/manifest_archivos.json).
    Devuelve None si no se localiza.
    """
    archivo = (p.proveedor.archivo_pdf or "") if p.proveedor else ""
    if not archivo:
        return None
    proyecto = Path(__file__).parent.parent

    # Estrategia 1: PDF_INGEST_DIR (flujo nuevo, .meta.json guarda el PDF original)
    candidato = _pdf_ingest_dir() / archivo
    if candidato.exists():
        return {
            "ruta": candidato,
            "canonico": str(candidato.relative_to(proyecto)).replace("\\", "/"),
            "tipo": candidato.suffix.lower(),
            "fuente": "pdf_ingest_dir",
        }

    # Estrategia 2: manifest legacy
    import json as _json
    manifest_path = proyecto / "data" / "manifest_archivos.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return None
    base = archivo.lower().replace(".xlsx", "")
    for e in manifest.get("entradas", []):
        inter = (e.get("intermedio") or "").lower().replace(".xlsx", "")
        if inter and (inter == base or base in inter or inter in base):
            canonico = e["canonico"]
            ruta = proyecto / canonico
            if ruta.exists():
                return {
                    "ruta": ruta,
                    "canonico": canonico,
                    "tipo": e.get("tipo") or ruta.suffix.lower(),
                    "fuente": "manifest",
                }
            return None
    return None


@router.get("/api/productos/{producto_id}/origen")
def origen_cotizacion(producto_id: int, db: SesionDep):
    """Devuelve metadatos del archivo origen (PDF/xlsx) del producto.

    Estrategia:
      1. archivo_pdf del proveedor + PDF_INGEST_DIR (flujo nuevo).
      2. manifest legacy (data/manifest_archivos.json) como fallback.
    """
    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")

    archivo = (p.proveedor.archivo_pdf or "") if p.proveedor else ""
    if not archivo:
        return {"existe": False, "razon": "Proveedor sin archivo_pdf asociado"}

    info = _localizar_origen(p)
    if info is None:
        return {
            "existe": False,
            "razon": f"No se encontro el archivo '{archivo}' en {_pdf_ingest_dir().name}/ ni en manifest",
            "archivo_pdf": archivo,
        }
    # PDFs van al visor HTML wrapper (fuerza render inline aunque el navegador
    # tenga "descargar PDFs" activado). Otros tipos van directo al archivo.
    if info["tipo"] == ".pdf":
        ver_url = f"/visor-cotizacion/{producto_id}"
    else:
        ver_url = f"/cotizacion-original/{producto_id}"
    return {
        "existe": True,
        "canonico": info["canonico"],
        "tipo": info["tipo"],
        "fuente": info["fuente"],
        "ver_url": ver_url,
    }


@router.get("/visor-cotizacion/{producto_id}", response_class=HTMLResponse)
def visor_cotizacion(producto_id: int, db: SesionDep):
    """HTML wrapper que renderiza el PDF en un <embed> a tamano completo.

    Sirve para forzar visualizacion inline aun cuando el navegador del usuario
    tiene configurado "siempre descargar PDFs". El embed lo trata como recurso
    embebido, no como navegacion top-level.
    """
    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")
    info = _localizar_origen(p)
    if info is None:
        raise HTTPException(404, "Archivo origen no localizado")
    nombre = info["ruta"].name
    sku = (p.sku or f"producto-{p.id}")
    titulo = f"{sku} — {nombre}"
    pdf_url = f"/cotizacion-original/{producto_id}"
    descarga_url = f"/cotizacion-original/{producto_id}?descargar=1"
    html = f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>{titulo}</title>
  <style>
    html, body {{ margin:0; padding:0; height:100%; background:#222; font-family:system-ui,sans-serif; }}
    .barra {{ background:#111; color:#eee; padding:6px 12px; display:flex; gap:12px; align-items:center; font-size:12px; }}
    .barra .titulo {{ flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; opacity:.85; }}
    .barra a {{ color:#7cb8ff; text-decoration:none; padding:2px 8px; border:1px solid #335; border-radius:4px; }}
    .barra a:hover {{ background:#223; }}
    embed, iframe {{ width:100%; height:calc(100% - 32px); border:0; }}
  </style>
</head>
<body>
  <div class="barra">
    <span class="titulo" title="{titulo}">{titulo}</span>
    <a href="{descarga_url}" title="Descargar PDF">⬇ Descargar</a>
    <a href="javascript:window.close()" title="Cerrar ventana">✕ Cerrar</a>
  </div>
  <embed src="{pdf_url}" type="application/pdf">
</body>
</html>"""
    return HTMLResponse(html)


@router.get("/cotizacion-original/{producto_id}")
def ver_cotizacion_original(producto_id: int, db: SesionDep, descargar: int = 0):
    """Sirve el archivo origen del producto.

    PDFs van con Content-Disposition: inline para que se rendericen en el
    visor del navegador en vez de descargarse. xlsx/otros van como attachment.
    """
    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")

    info = _localizar_origen(p)
    if info is None:
        raise HTTPException(404, "Archivo origen no localizado")
    ruta = info["ruta"]
    tipo = info["tipo"]
    if tipo == ".pdf":
        # ?descargar=1 fuerza attachment; default es inline para que se
        # renderice en el <embed> del visor.
        # Usamos content_disposition_type + filename para que starlette arme
        # el header con encoding RFC 5987 (filename*=UTF-8'') — necesario
        # cuando el nombre tiene caracteres no-latin-1 (ej. chino).
        disposition = "attachment" if descargar else "inline"
        return FileResponse(
            str(ruta),
            media_type="application/pdf",
            filename=ruta.name,
            content_disposition_type=disposition,
        )
    return FileResponse(str(ruta), filename=ruta.name, media_type="application/octet-stream")


@router.get("/api/productos/{producto_id}/cotizar")
def cotizar_14_pasos(
    producto_id: int,
    db: SesionDep,
    tc: float | None = Query(None, description="Tipo de cambio MXN/USD override"),
    margen_nuestro: float | None = Query(None, description="Margen Lloyds (0-100)"),
    margen_cliente: float | None = Query(None, description="Margen retailer (0-100)"),
    flete_maritimo_usd: float | None = Query(None, description="Flete maritimo USD/contenedor"),
    flete_local_mxn: float | None = Query(None, description="Flete local MXN por contenedor"),
    descuentos_pct: float | None = Query(None, description="Descuentos comerciales % (0-100)"),
    descuentos_na_pct: float | None = Query(None, description="Descuentos no aplicables % (0-100)"),
    gasto_fijo_pct: float | None = Query(None, description="Gastos fijos % (0-100)"),
    gastos_aduanales_pct: float | None = Query(None, description="Gastos aduanales % (0-100). 0 desactiva el paso 6."),
    piezas: int | None = Query(None, description="Piezas/40HQ override"),
):
    """Devuelve los 14 pasos del motor de cotizacion para un producto."""
    from app.cotizador.adapter import producto_a_row
    from app.cotizador.engine import compute_for_row, STEP_LABELS

    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")

    row = producto_a_row(p)

    # Sumar costos adicionales (caja color, EXW->FOB, etc.) al FOB efectivo
    costos = (
        db.query(CostoAdicional)
        .filter_by(producto_id=producto_id)
        .all()
    )
    suma_costos = sum(c.monto_usd for c in costos)
    fob_original = row["unit_price"] or 0
    if suma_costos > 0:
        row["unit_price"] = fob_original + suma_costos

    # Resolver arancel via DB overrides + default rule (acero=35%, otros=25%)
    from app.cotizador.lookup import resolver_arancel
    arancel = resolver_arancel(db, p.categoria, p.subcategoria, p.material)

    # Settings: flete local default 70k MXN (Salo lo confirmo). Descuentos
    # y gastos fijos editables. Cualquier override del query string gana.
    settings = {"flete_local_mxn": flete_local_mxn if flete_local_mxn is not None else 70000}
    if descuentos_pct is not None:
        settings["descuentos_pct"] = descuentos_pct
    if descuentos_na_pct is not None:
        settings["descuentos_na_pct"] = descuentos_na_pct
    if gasto_fijo_pct is not None:
        settings["gasto_fijo_pct"] = gasto_fijo_pct
    if gastos_aduanales_pct is not None:
        settings["gastos_aduanales_pct"] = gastos_aduanales_pct

    res = compute_for_row(
        row,
        settings=settings,
        override_tc=tc,
        override_piezas_contenedor=piezas,
        override_flete_maritimo_usd=flete_maritimo_usd,
        margen_nuestro_pct=margen_nuestro,
        margen_cliente_pct=margen_cliente,
        override_tasa_pct=arancel.tasa_pct,
    )
    # Reemplazar la fraccion del result con la resuelta por nuestro lookup
    # (PricingResult es frozen; usamos dataclass.replace)
    from dataclasses import replace as _replace
    res = _replace(res, fraccion_arancelaria=arancel.fraccion)

    # Parametros pais para que el frontend pueda invertir el calculo
    # (editar retail -> calcular margen efectivo)
    from app.cotizador.defaults import country_params, DEFAULTS
    cp = country_params(res.country_code)
    total_desc_pct = float(cp["descuentos_pct"]) + float(cp["descuentos_na_pct"]) + float(cp["gasto_fijo_pct"])

    return {
        "producto_id": producto_id,
        "sku": p.sku,
        "descripcion": p.descripcion,
        "categoria": p.categoria,
        "subcategoria": p.subcategoria,
        "fob_usd": p.fob_usd,
        "fob_efectivo_usd": fob_original + suma_costos if suma_costos > 0 else p.fob_usd,
        "costos_adicionales_total_usd": suma_costos,
        "costos_adicionales": [_costo_to_dict(c) for c in costos],
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
        "pzas_caja": p.pzas_caja,
        "nw_caja_kg": p.nw_caja_kg,
        "gw_caja_kg": p.gw_caja_kg,
        "lead_time": p.lead_time,
        "item_type": p.item_type or "Primary",
        "proveedor": p.proveedor.nombre if p.proveedor else None,
        "fotos": [f.ruta_relativa for f in p.fotos],
        "fraccion_arancelaria": res.fraccion_arancelaria,
        "tasa_arancelaria_pct": str(res.tasa_arancelaria_pct),
        "tasa_arancelaria_fuente": arancel.fuente,
        "tasa_arancelaria_nota": arancel.nota,
        "tipo_cambio": str(res.tipo_cambio),
        "margen_nuestro": str(res.margen_nuestro_effective),
        "margen_cliente": str(res.margen_cliente_effective),
        # Para invertir el calculo en el frontend
        "iva_pct": float(cp["iva_pct"]),
        "total_desc_pct": total_desc_pct,
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
        nueva_cat = body.categoria.strip() or None
        p.categoria = nueva_cat
        # Auto-registrar la categoria en el catalogo si es nueva. Asi la UI
        # y la BD quedan sincronizadas: cualquier valor visible en el
        # dropdown existe en la tabla 'categorias' (con orden=999 y sin
        # keywords; queda como manual hasta que se le agreguen via YAML).
        # No aplica al sentinela '_descartar' (no es categoria real).
        if nueva_cat and nueva_cat != "_descartar":
            existe = db.query(Categoria).filter_by(slug=nueva_cat).first()
            if existe is None:
                db.add(Categoria(slug=nueva_cat, orden=999))
    if body.subcategoria is not None:
        p.subcategoria = body.subcategoria.strip() or None
    # Campos del producto editables desde el panel de detalle.
    # Para strings: cadena vacia significa "limpiar" (None en BD).
    # Para numericos: usar None explicito en el JSON para limpiar.
    if body.material is not None:
        p.material = body.material.strip() or None
    if body.medidas is not None:
        p.medidas = body.medidas.strip() or None
    if body.peso_kg is not None:
        p.peso_kg = body.peso_kg
    if body.color is not None:
        p.color = body.color.strip() or None
    if body.moq is not None:
        p.moq = body.moq.strip() or None
    if body.packing is not None:
        p.packing = body.packing.strip() or None
    if body.carton_dims is not None:
        p.carton_dims = body.carton_dims.strip() or None
    if body.cbm is not None:
        p.cbm = body.cbm
    if body.pzas_20ft is not None:
        p.pzas_20ft = body.pzas_20ft
    if body.pzas_40hq is not None:
        p.pzas_40hq = body.pzas_40hq
    if body.pzas_caja is not None:
        p.pzas_caja = body.pzas_caja
    if body.nw_caja_kg is not None:
        p.nw_caja_kg = body.nw_caja_kg
    if body.gw_caja_kg is not None:
        p.gw_caja_kg = body.gw_caja_kg
    # Auto-derive pzas_caja desde N.W./peso_unit cuando esta vacio.
    # Se dispara si el editor toco nw_caja_kg o peso_kg y pzas_caja sigue
    # en blanco. No sobrescribe un valor ya capturado: si el operador
    # quiere forzar el recalculo, debe primero limpiar pzas_caja (=null).
    if (body.nw_caja_kg is not None or body.peso_kg is not None) \
       and (not p.pzas_caja or p.pzas_caja <= 0):
        derivado = pzas_caja_desde_nw_y_peso(p.nw_caja_kg, p.peso_kg)
        if derivado:
            p.pzas_caja = derivado
    if body.lead_time is not None:
        p.lead_time = body.lead_time.strip() or None
    if body.item_type is not None:
        p.item_type = body.item_type.strip() or "Primary"
    db.commit()
    return {"ok": True}


class CbmAplicarBody(BaseModel):
    ids: list[int]


@router.get("/api/cbm/sugerencias")
def cbm_sugerencias(db: SesionDep):
    """Lista productos con CBM faltante o discrepante respecto a carton_dims.

    Usa el mismo criterio que el auto-derive del ingest: si cbm es None/<=0 y
    carton_dims parsea, sugiere el calculado. Si cbm difiere >50% del calculado,
    marca como discrepancia.
    """
    productos = db.query(Producto).all()
    items = []
    for p in productos:
        cbm_calc = cbm_desde_carton_dims(p.carton_dims)
        if cbm_calc is None or cbm_calc <= 0:
            continue
        cbm_db = p.cbm
        if cbm_db is None or cbm_db <= 0:
            estado = "falta"
        elif cbm_es_discrepante(cbm_db, cbm_calc):
            estado = "discrepancia"
        else:
            continue
        items.append({
            "producto_id": p.id,
            "sku": p.sku,
            "descripcion": p.descripcion,
            "carton_dims": p.carton_dims,
            "cbm_actual": cbm_db,
            "cbm_calculado": cbm_calc,
            "estado": estado,
        })
    return {"items": items, "total": len(items)}


@router.post("/api/cbm/aplicar")
def cbm_aplicar(body: CbmAplicarBody, db: SesionDep):
    """Aplica el CBM calculado desde carton_dims a los productos indicados.

    No-op silencioso si el producto no tiene carton_dims parseable: queda
    fuera del array `aplicados` para que la UI muestre la situacion.
    """
    aplicados = []
    sin_cambio = []
    for pid in body.ids:
        p = db.get(Producto, pid)
        if not p:
            continue
        cbm_calc = cbm_desde_carton_dims(p.carton_dims)
        if cbm_calc is None or cbm_calc <= 0:
            sin_cambio.append({"producto_id": pid, "motivo": "carton_dims no parseable"})
            continue
        aplicados.append({
            "producto_id": pid,
            "cbm_anterior": p.cbm,
            "cbm_nuevo": cbm_calc,
        })
        p.cbm = cbm_calc
    db.commit()
    return {"ok": True, "aplicados": aplicados, "sin_cambio": sin_cambio}


class Pzas40hqAplicarBody(BaseModel):
    ids: list[int]


@router.get("/api/pzas40hq/sugerencias")
def pzas40hq_sugerencias(db: SesionDep):
    """Lista productos con pzas_40hq faltante. Incluye bloqueados (sin inputs).

    Estado 'falta': pzas_40hq vacio + tiene cbm + pzas_caja -> aplicable.
    Estado 'bloqueado': pzas_40hq vacio pero falta cbm o pzas_caja -> no
    se puede derivar; la UI lo muestra con motivo para que el usuario sepa
    que tiene que llenar el input previo (o re-ingestar).

    No se reportan productos con pzas_40hq ya poblado: respetamos lo que
    Claude leyo directo del PDF.
    """
    productos = db.query(Producto).all()
    items = []
    for p in productos:
        if p.pzas_40hq and p.pzas_40hq > 0:
            continue
        calc = pzas_40hq_desde_cbm_y_caja(p.cbm, p.pzas_caja)
        if calc is not None and calc > 0:
            estado = "falta"
            motivo = None
        else:
            estado = "bloqueado"
            faltan = []
            if not p.cbm or p.cbm <= 0:
                faltan.append("cbm")
            if not p.pzas_caja or p.pzas_caja <= 0:
                faltan.append("pzas_caja")
            motivo = "falta " + " + ".join(faltan) if faltan else "sin inputs"
        items.append({
            "producto_id": p.id,
            "sku": p.sku,
            "descripcion": p.descripcion,
            "cbm": p.cbm,
            "pzas_caja": p.pzas_caja,
            "pzas_40hq_actual": p.pzas_40hq,
            "pzas_40hq_calculado": calc,
            "estado": estado,
            "motivo": motivo,
        })
    return {"items": items, "total": len(items)}


@router.post("/api/pzas40hq/aplicar")
def pzas40hq_aplicar(body: Pzas40hqAplicarBody, db: SesionDep):
    """Aplica pzas_40hq calculado a los productos indicados.

    Skip silencioso si faltan inputs (cbm o pzas_caja): el id queda fuera
    de `aplicados` para que la UI muestre la situacion.
    """
    aplicados = []
    sin_cambio = []
    for pid in body.ids:
        p = db.get(Producto, pid)
        if not p:
            continue
        calc = pzas_40hq_desde_cbm_y_caja(p.cbm, p.pzas_caja)
        if calc is None or calc <= 0:
            faltan = []
            if not p.cbm or p.cbm <= 0:
                faltan.append("cbm")
            if not p.pzas_caja or p.pzas_caja <= 0:
                faltan.append("pzas_caja")
            sin_cambio.append({
                "producto_id": pid,
                "motivo": "falta " + " + ".join(faltan) if faltan else "no aplicable",
            })
            continue
        aplicados.append({
            "producto_id": pid,
            "pzas_40hq_anterior": p.pzas_40hq,
            "pzas_40hq_nuevo": calc,
        })
        p.pzas_40hq = calc
    db.commit()
    return {"ok": True, "aplicados": aplicados, "sin_cambio": sin_cambio}


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
        request,
        "productos.html",
        {
            "productos": productos,
            "categorias": categorias,
        },
    )


def _correr_llenar_formato_hd(
    db: Session,
    xlsx_int: Path,
    salida: Path,
    categoria: str | None = "__usar_marcados__",
    params: dict | None = None,
) -> Path:
    """Genera intermedio (por marcas o por categoria) + corre llenar_formato_hd.py.

    Si categoria == '__usar_marcados__' (default): filtra por marcado_cotizar=True.
    Si categoria es None: filtra productos sin categoria.
    Si categoria es str: filtra por esa categoria.

    `params` se pasa a _cotizar_producto como fallback cuando un producto no
    tiene snapshot guardado: TC, margenes, fletes, descuentos de la barra UI.

    Devuelve la ruta del archivo HD producido. Lanza HTTPException en error.
    """
    import os
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
            params=params,
        )
        if n == 0:
            raise HTTPException(400, "No hay productos marcados.")
    else:
        n = generar_formato_hd_por_categoria(
            session=db,
            xlsx_intermedio=str(xlsx_int),
            base_fotos=str(proyecto / "data"),
            categoria=categoria,
            params=params,
        )
        if n == 0:
            raise HTTPException(404, f"No hay productos en categoria {categoria!r}.")

    formato_env = os.environ.get("FORMATO_HD_PATH")
    formato = Path(formato_env) if formato_env else proyecto / "Pet Quote Sheet 2026.xlsb"
    script = proyecto / "llenar_formato_hd.py"

    # Replicar la logica de naming de llenar_formato_hd.construir_nombre_salida():
    # toma stem del intermedio en minusculas y quita el prefijo '_intermedio_'.
    base_salida = xlsx_int.stem.lower()
    if base_salida.startswith("_intermedio_"):
        base_salida = base_salida[len("_intermedio_"):]
    salida_default = proyecto / f"formato-hd-{base_salida}.xlsx"

    # Borrar todas las salidas posibles previas (default + custom + variantes)
    for p in {salida, salida_default}:
        if p.exists():
            try:
                p.unlink()
            except PermissionError:
                raise HTTPException(
                    500,
                    f"No puedo borrar el archivo anterior (esta abierto en Excel?): {p.name}",
                )

    # El intermedio de exportar.py tiene 18 columnas (Foto=A, SKU=B, Descripcion=C,
    # ..., Margen Lloyds=Q, Proveedor=R).
    # El default de llenar_formato_hd.py asume el layout viejo (22 cols), por
    # eso forzamos el mapeo.
    result = subprocess.run(
        [
            _sys.executable, str(script), str(xlsx_int), str(formato),
            # Mapeo correcto al HD destino:
            #   col B (SKU)         -> fila 5  (SKU (# or TBD))
            #   col C (Descripcion) -> fila 8  (DESCRIPTION)
            #   col H (MOQ)         -> fila 12 (MOQ Domestic)
            #   col M (Pzas 40hq)   -> fila 15 (Pieces per Container)
            #   col O (Venta HD)    -> fila 11 (DOMESTIC COST)
            #   col P (Retail MXN)  -> fila 16 (SUGGESTED RETAIL)
            #   col Q (Margen)      -> fila 17 (THD MARGIN)
            # Fila 4 (Vendor Number) se deja en "TBD" via CONSTANTES,
            # no se mapea el proveedor (R) al HD.
            "--mapeo", "B=5,C=8,H=12,M=15,O=11,P=16,Q=17", "--yes",
        ],
        capture_output=True, text=True, cwd=str(proyecto),
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise HTTPException(500, f"Fallo llenar_formato_hd: {result.stderr[:500]}")

    # llenar_formato_hd.py escribe a salida_default; si nos pidieron otro nombre, renombrar.
    if not salida_default.exists():
        raise HTTPException(500, f"No encontre el archivo de salida: {salida_default.name}")
    if salida_default != salida:
        salida_default.replace(salida)
    return salida


def _snapshot_productos_exportados(
    db: Session,
    productos_q,
    archivo_nombre: str,
    origen: str,
    params: dict | None = None,
) -> int:
    """Crea un CotizacionSnapshot por cada producto en la query exportada.

    Importante: preserva el retail_final_mxn del snapshot manual mas
    reciente del producto. Asi exportar varias veces no pierde el retail
    que el usuario edito explicitamente al guardar la cotizacion.
    """
    params = params or {}
    n = 0
    for p in productos_q.all():
        try:
            # Buscar el retail editado mas reciente del producto (de cualquier
            # snapshot previo). Sin esto, el auto-snapshot del export pisaria
            # el retail editado del usuario con el paso 13 del motor.
            ultimo_snap = (
                db.query(CotizacionSnapshot)
                .filter_by(producto_id=p.id)
                .order_by(CotizacionSnapshot.creado_en.desc())
                .first()
            )
            retail_preservado = ultimo_snap.retail_final_mxn if ultimo_snap else None

            _crear_snapshot(
                db, p.id,
                origen=origen,
                archivo_exportado=archivo_nombre,
                tc=params.get("tc"),
                margen_nuestro_pct=params.get("margen_nuestro_pct"),
                margen_cliente_pct=params.get("margen_cliente_pct"),
                flete_maritimo_usd=params.get("flete_maritimo_usd"),
                flete_local_mxn=params.get("flete_local_mxn"),
                descuentos_pct=params.get("descuentos_pct"),
                descuentos_na_pct=params.get("descuentos_na_pct"),
                gasto_fijo_pct=params.get("gasto_fijo_pct"),
                gastos_aduanales_pct=params.get("gastos_aduanales_pct"),
                retail_final_mxn=retail_preservado,
            )
            n += 1
        except Exception:
            # Un fallo individual no debe abortar el export
            db.rollback()
    return n


def _params_exportar(
    tc: float | None = Query(None),
    margen_nuestro_pct: float | None = Query(None, description="Escala 0-100"),
    margen_cliente_pct: float | None = Query(None, description="Escala 0-100"),
    flete_maritimo_usd: float | None = Query(None),
    flete_local_mxn: float | None = Query(None),
    descuentos_pct: float | None = Query(None),
    descuentos_na_pct: float | None = Query(None),
    gasto_fijo_pct: float | None = Query(None),
    gastos_aduanales_pct: float | None = Query(None),
) -> dict:
    """Recolecta params del query string como fallback del snapshot.

    Se inyecta como dependencia via Depends() en los endpoints de export.
    Solo incluye claves con valor != None para no pisar defaults.
    """
    raw = {
        "tc": tc,
        "margen_nuestro_pct": margen_nuestro_pct,
        "margen_cliente_pct": margen_cliente_pct,
        "flete_maritimo_usd": flete_maritimo_usd,
        "flete_local_mxn": flete_local_mxn,
        "descuentos_pct": descuentos_pct,
        "descuentos_na_pct": descuentos_na_pct,
        "gasto_fijo_pct": gasto_fijo_pct,
        "gastos_aduanales_pct": gastos_aduanales_pct,
    }
    return {k: v for k, v in raw.items() if v is not None}


@router.get("/exportar")
def exportar(
    db: SesionDep,
    params: dict = Depends(_params_exportar),
):
    """Genera HD desde la seleccion actual (compatibilidad)."""
    proyecto = Path(__file__).parent.parent
    xlsx_int = proyecto / "_intermedio_seleccion.xlsx"
    salida = proyecto / f"formato-hd-{xlsx_int.stem.lower()}.xlsx"
    archivo = _correr_llenar_formato_hd(db, xlsx_int, salida, params=params)
    # Snapshot por cada producto marcado
    _snapshot_productos_exportados(
        db,
        db.query(Producto).filter(Producto.marcado_cotizar.is_(True)),
        archivo.name,
        origen="export-marcados",
        params=params,
    )
    return FileResponse(str(archivo), filename=archivo.name)


@router.get("/exportar-interno")
def exportar_interno(
    db: SesionDep,
    params: dict = Depends(_params_exportar),
):
    """Genera xlsx vertical con TODAS las columnas (foto, FOB, costos, arancel,
    landing, venta HD, retail, margenes) de los productos marcados. Uso interno.
    """
    from app.exportar import generar_export_interno_marcados

    proyecto = Path(__file__).parent.parent
    fecha = date.today().strftime("%Y%m%d")
    salida = proyecto / f"cotizacion-interna-{fecha}.xlsx"
    if salida.exists():
        try:
            salida.unlink()
        except PermissionError:
            raise HTTPException(
                500,
                f"No puedo borrar el archivo anterior (esta abierto en Excel?): {salida.name}",
            )

    n = generar_export_interno_marcados(
        session=db,
        xlsx_salida=str(salida),
        base_fotos=str(proyecto / "data"),
        params=params,
    )
    if n == 0:
        raise HTTPException(400, "No hay productos marcados.")
    # Snapshot por producto exportado, igual que el HD
    _snapshot_productos_exportados(
        db,
        db.query(Producto).filter(Producto.marcado_cotizar.is_(True)),
        salida.name,
        origen="export-interno",
        params=params,
    )
    return FileResponse(str(salida), filename=salida.name)


@router.get("/exportar/{categoria}")
def exportar_categoria(
    categoria: str,
    db: SesionDep,
    params: dict = Depends(_params_exportar),
):
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
    archivo = _correr_llenar_formato_hd(db, xlsx_int, salida, categoria=cat_filter, params=params)
    # Snapshot por cada producto exportado en la categoria
    _snapshot_productos_exportados(
        db,
        _aplicar_filtro_categoria(db.query(Producto), categoria),
        archivo.name,
        origen=f"export-cat:{cat_slug}",
        params=params,
    )
    return FileResponse(str(archivo), filename=archivo.name)


@router.get("/exportar-pd")
def exportar_pd(db: SesionDep):
    """Genera el formato Pet PD (Product Dimension) con productos marcados.

    No usa el motor de cotizacion (no toma TC/margenes/etc): solo datos
    fisicos del producto. Por eso no recibe params.
    """
    from app.exportar import generar_export_pd_desde_marcados

    proyecto = Path(__file__).parent.parent
    fecha = date.today().strftime("%Y%m%d")
    salida = proyecto / f"export-pd-marcados-{fecha}.xlsx"
    if salida.exists():
        try:
            salida.unlink()
        except PermissionError:
            raise HTTPException(
                500,
                f"No puedo borrar el archivo anterior (esta abierto en Excel?): {salida.name}",
            )
    n = generar_export_pd_desde_marcados(db, str(salida), str(proyecto / "data"))
    if n == 0:
        raise HTTPException(400, "No hay productos marcados.")
    return FileResponse(
        str(salida),
        filename=salida.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/exportar-pd/{categoria}")
def exportar_pd_categoria(categoria: str, db: SesionDep):
    """Genera Pet PD para una categoria sin tocar marcas.

    Si categoria == '__sin_categoria__', exporta productos sin categoria.
    """
    from app.exportar import generar_export_pd_por_categoria

    proyecto = Path(__file__).parent.parent
    cat_filter = None if categoria == SIN_CATEGORIA else categoria
    cat_slug = "sin-categoria" if cat_filter is None else categoria
    fecha = date.today().strftime("%Y%m%d")
    salida = proyecto / f"export-pd-{cat_slug}-{fecha}.xlsx"
    if salida.exists():
        try:
            salida.unlink()
        except PermissionError:
            raise HTTPException(
                500,
                f"No puedo borrar el archivo anterior (esta abierto en Excel?): {salida.name}",
            )
    n = generar_export_pd_por_categoria(db, str(salida), str(proyecto / "data"), cat_filter)
    if n == 0:
        raise HTTPException(404, f"No hay productos en categoria {categoria!r}.")
    return FileResponse(
        str(salida),
        filename=salida.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ============================================================
# Aranceles override: CRUD + pagina
# ============================================================


class ArancelBody(BaseModel):
    categoria: str | None = None
    material_pattern: str | None = None
    fraccion: str
    tasa_pct: float
    nota: str | None = None


def _arancel_to_dict(o: ArancelOverride) -> dict:
    return {
        "id": o.id,
        "categoria": o.categoria,
        "material_pattern": o.material_pattern,
        "fraccion": o.fraccion,
        "tasa_pct": o.tasa_pct,
        "nota": o.nota,
    }


@router.get("/api/aranceles")
def listar_aranceles(db: SesionDep):
    rows = db.query(ArancelOverride).order_by(
        ArancelOverride.categoria.is_(None),  # nulls al final
        ArancelOverride.categoria,
        ArancelOverride.material_pattern,
    ).all()
    return {"items": [_arancel_to_dict(o) for o in rows]}


@router.post("/api/aranceles")
def crear_arancel(body: ArancelBody, db: SesionDep):
    o = ArancelOverride(
        categoria=(body.categoria or None) or None,
        material_pattern=(body.material_pattern or None) or None,
        fraccion=body.fraccion.strip(),
        tasa_pct=body.tasa_pct,
        nota=(body.nota or None),
    )
    # Normalizar: strings vacios a NULL
    if o.categoria == "":
        o.categoria = None
    if o.material_pattern == "":
        o.material_pattern = None
    db.add(o)
    db.commit()
    db.refresh(o)
    return _arancel_to_dict(o)


@router.patch("/api/aranceles/{arancel_id}")
def actualizar_arancel(arancel_id: int, body: ArancelBody, db: SesionDep):
    o = db.get(ArancelOverride, arancel_id)
    if not o:
        raise HTTPException(404, "Arancel no existe")
    o.categoria = (body.categoria or None) or None
    o.material_pattern = (body.material_pattern or None) or None
    o.fraccion = body.fraccion.strip()
    o.tasa_pct = body.tasa_pct
    o.nota = body.nota or None
    if o.categoria == "":
        o.categoria = None
    if o.material_pattern == "":
        o.material_pattern = None
    db.commit()
    return _arancel_to_dict(o)


@router.delete("/api/aranceles/{arancel_id}")
def eliminar_arancel(arancel_id: int, db: SesionDep):
    o = db.get(ArancelOverride, arancel_id)
    if not o:
        raise HTTPException(404, "Arancel no existe")
    db.delete(o)
    db.commit()
    return {"ok": True}


# ============================================================
# Aranceles ESTANDAR (tabla `aranceles`, seedeada desde YAML)
# Estos son los fallback por (cat, subcat) que antes vivian en
# app/cotizador/tariffs.py. Editables desde la UI.
# ============================================================


class ArancelEstandarBody(BaseModel):
    categoria: str
    subcategoria: str
    fraccion: str
    tasa_pct: float
    nota: str | None = None


def _arancel_estandar_to_dict(a: Arancel) -> dict:
    return {
        "id": a.id,
        "categoria": a.categoria,
        "subcategoria": a.subcategoria,
        "fraccion": a.fraccion,
        "tasa_pct": a.tasa_pct,
        "nota": a.nota,
    }


@router.get("/api/aranceles-estandar")
def listar_aranceles_estandar(db: SesionDep):
    rows = (
        db.query(Arancel)
        .order_by(Arancel.categoria, Arancel.subcategoria)
        .all()
    )
    return {"items": [_arancel_estandar_to_dict(a) for a in rows]}


@router.post("/api/aranceles-estandar")
def crear_arancel_estandar(body: ArancelEstandarBody, db: SesionDep):
    cat = body.categoria.strip()
    sub = body.subcategoria.strip()
    if not cat or not sub:
        raise HTTPException(400, "categoria y subcategoria son obligatorias")
    existe = db.query(Arancel).filter_by(categoria=cat, subcategoria=sub).first()
    if existe is not None:
        raise HTTPException(409, f"Ya existe ({cat}, {sub})")
    a = Arancel(
        categoria=cat,
        subcategoria=sub,
        fraccion=body.fraccion.strip(),
        tasa_pct=body.tasa_pct,
        nota=(body.nota or None),
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return _arancel_estandar_to_dict(a)


@router.patch("/api/aranceles-estandar/{arancel_id}")
def actualizar_arancel_estandar(arancel_id: int, body: ArancelEstandarBody, db: SesionDep):
    a = db.get(Arancel, arancel_id)
    if not a:
        raise HTTPException(404, "Arancel estandar no existe")
    a.categoria = body.categoria.strip()
    a.subcategoria = body.subcategoria.strip()
    a.fraccion = body.fraccion.strip()
    a.tasa_pct = body.tasa_pct
    a.nota = (body.nota or None)
    db.commit()
    return _arancel_estandar_to_dict(a)


@router.delete("/api/aranceles-estandar/{arancel_id}")
def eliminar_arancel_estandar(arancel_id: int, db: SesionDep):
    a = db.get(Arancel, arancel_id)
    if not a:
        raise HTTPException(404, "Arancel estandar no existe")
    db.delete(a)
    db.commit()
    return {"ok": True}


# ============================================================
# Cotizacion snapshots (historial)
# ============================================================


class SnapshotBody(BaseModel):
    origen: str = "manual"
    # Settings tal como estan en el UI (vienen en escala 0-100 para %)
    tc: float | None = None
    flete_maritimo_usd: float | None = None
    flete_local_mxn: float | None = None
    margen_nuestro_pct: float | None = None
    margen_cliente_pct: float | None = None
    descuentos_pct: float | None = None
    descuentos_na_pct: float | None = None
    gasto_fijo_pct: float | None = None
    gastos_aduanales_pct: float | None = None
    # Retail final (opcional: si se omite usa paso 13 del motor)
    retail_final_mxn: float | None = None
    archivo_exportado: str | None = None
    notas: str | None = None


def _snapshot_to_dict(s: CotizacionSnapshot, producto: Producto | None = None) -> dict:
    """Serializa un snapshot. Si se pasa `producto`, calcula `es_stale`:
    True cuando el producto fue modificado despues de creado el snapshot."""
    es_stale = False
    if producto is not None and producto.actualizado_en and s.creado_en:
        es_stale = producto.actualizado_en > s.creado_en
    return {
        "id": s.id,
        "producto_id": s.producto_id,
        "creado_en": s.creado_en.isoformat() if s.creado_en else None,
        "origen": s.origen,
        "fob_usd_efectivo": s.fob_usd_efectivo,
        "costos_adicionales_usd": s.costos_adicionales_usd,
        "tc": s.tc,
        "flete_maritimo_usd": s.flete_maritimo_usd,
        "flete_local_mxn": s.flete_local_mxn,
        "margen_nuestro_pct": s.margen_nuestro_pct,
        "margen_cliente_pct": s.margen_cliente_pct,
        "descuentos_pct": s.descuentos_pct,
        "descuentos_na_pct": s.descuentos_na_pct,
        "gasto_fijo_pct": s.gasto_fijo_pct,
        "gastos_aduanales_pct": s.gastos_aduanales_pct,
        "fraccion_arancelaria": s.fraccion_arancelaria,
        "tasa_arancelaria_pct": s.tasa_arancelaria_pct,
        "landed_unit_mxn": s.landed_unit_mxn,
        "venta_lloyds_mxn": s.venta_lloyds_mxn,
        "retail_final_mxn": s.retail_final_mxn,
        "margen_real_pct": s.margen_real_pct,
        "archivo_exportado": s.archivo_exportado,
        "notas": s.notas,
        "es_stale": es_stale,
    }


def _crear_snapshot(
    db: Session,
    producto_id: int,
    *,
    origen: str,
    tc: float | None = None,
    flete_maritimo_usd: float | None = None,
    flete_local_mxn: float | None = None,
    margen_nuestro_pct: float | None = None,
    margen_cliente_pct: float | None = None,
    descuentos_pct: float | None = None,
    descuentos_na_pct: float | None = None,
    gasto_fijo_pct: float | None = None,
    gastos_aduanales_pct: float | None = None,
    retail_final_mxn: float | None = None,
    archivo_exportado: str | None = None,
    notas: str | None = None,
) -> CotizacionSnapshot:
    """Construye y persiste un snapshot corriendo el motor con los settings dados.

    Si retail_final_mxn viene, lo usa para derivar margen_real inverso.
    Si no, usa paso 13 del motor.
    """
    from app.cotizador.engine import compute_for_row
    from app.cotizador.adapter import producto_a_row
    from app.cotizador.lookup import resolver_arancel
    from app.cotizador.defaults import country_params

    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")

    # Sumar costos adicionales
    costos = db.query(CostoAdicional).filter_by(producto_id=producto_id).all()
    suma_costos = sum(c.monto_usd for c in costos)
    row = producto_a_row(p)
    fob_original = row["unit_price"] or 0
    if suma_costos > 0:
        row["unit_price"] = fob_original + suma_costos

    arancel = resolver_arancel(db, p.categoria, p.subcategoria, p.material)

    settings = {}
    if flete_local_mxn is not None:
        settings["flete_local_mxn"] = flete_local_mxn
    if descuentos_pct is not None:
        settings["descuentos_pct"] = descuentos_pct
    if descuentos_na_pct is not None:
        settings["descuentos_na_pct"] = descuentos_na_pct
    if gasto_fijo_pct is not None:
        settings["gasto_fijo_pct"] = gasto_fijo_pct
    if gastos_aduanales_pct is not None:
        settings["gastos_aduanales_pct"] = gastos_aduanales_pct

    res = compute_for_row(
        row,
        settings=settings,
        override_tc=tc,
        override_flete_maritimo_usd=flete_maritimo_usd,
        margen_nuestro_pct=margen_nuestro_pct,
        margen_cliente_pct=margen_cliente_pct,
        override_tasa_pct=arancel.tasa_pct,
    )

    landed = float(res.paso9)
    venta_motor = float(res.paso11)
    retail_motor = float(res.paso13)
    cp = country_params(res.country_code, settings=settings)
    iva = float(cp["iva_pct"]) / 100
    td = (float(cp["descuentos_pct"]) + float(cp["descuentos_na_pct"]) + float(cp["gasto_fijo_pct"])) / 100
    mc = float(res.margen_cliente_effective) / 100

    # Si vino retail_final, derivar venta inversa para calcular margen real
    if retail_final_mxn and retail_final_mxn > 0:
        venta_efectiva = (retail_final_mxn / (1 + iva)) * (1 - mc)
        retail_persistido = retail_final_mxn
    else:
        venta_efectiva = venta_motor
        retail_persistido = retail_motor

    # margen real = (venta - landing - venta*td) / venta
    if venta_efectiva > 0:
        margen_real = 1 - (landed / venta_efectiva) - td
    else:
        margen_real = 0

    snap = CotizacionSnapshot(
        producto_id=producto_id,
        origen=origen,
        fob_usd_efectivo=fob_original + suma_costos,
        costos_adicionales_usd=suma_costos,
        tc=tc,
        flete_maritimo_usd=flete_maritimo_usd,
        flete_local_mxn=settings.get("flete_local_mxn"),
        margen_nuestro_pct=margen_nuestro_pct,
        margen_cliente_pct=margen_cliente_pct,
        descuentos_pct=settings.get("descuentos_pct", float(cp["descuentos_pct"])),
        descuentos_na_pct=settings.get("descuentos_na_pct", float(cp["descuentos_na_pct"])),
        gasto_fijo_pct=settings.get("gasto_fijo_pct", float(cp["gasto_fijo_pct"])),
        gastos_aduanales_pct=settings.get("gastos_aduanales_pct"),
        fraccion_arancelaria=arancel.fraccion,
        tasa_arancelaria_pct=float(arancel.tasa_pct),
        landed_unit_mxn=landed,
        venta_lloyds_mxn=venta_efectiva,
        retail_final_mxn=retail_persistido,
        margen_real_pct=margen_real * 100,
        archivo_exportado=archivo_exportado,
        notas=notas,
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap


@router.get("/api/productos/{producto_id}/snapshots")
def listar_snapshots(producto_id: int, db: SesionDep):
    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")
    snaps = (
        db.query(CotizacionSnapshot)
        .filter_by(producto_id=producto_id)
        .order_by(CotizacionSnapshot.creado_en.desc())
        .all()
    )
    return {"items": [_snapshot_to_dict(s, p) for s in snaps]}


@router.post("/api/productos/{producto_id}/snapshots")
def crear_snapshot_manual(producto_id: int, body: SnapshotBody, db: SesionDep):
    snap = _crear_snapshot(
        db, producto_id,
        origen=body.origen or "manual",
        tc=body.tc,
        flete_maritimo_usd=body.flete_maritimo_usd,
        flete_local_mxn=body.flete_local_mxn,
        margen_nuestro_pct=body.margen_nuestro_pct,
        margen_cliente_pct=body.margen_cliente_pct,
        descuentos_pct=body.descuentos_pct,
        descuentos_na_pct=body.descuentos_na_pct,
        gasto_fijo_pct=body.gasto_fijo_pct,
        gastos_aduanales_pct=body.gastos_aduanales_pct,
        retail_final_mxn=body.retail_final_mxn,
        archivo_exportado=body.archivo_exportado,
        notas=body.notas,
    )
    p = db.get(Producto, producto_id)
    return _snapshot_to_dict(snap, p)


@router.delete("/api/productos/{producto_id}/snapshots/{snapshot_id}")
def borrar_snapshot(producto_id: int, snapshot_id: int, db: SesionDep):
    s = db.get(CotizacionSnapshot, snapshot_id)
    if not s or s.producto_id != producto_id:
        raise HTTPException(404, "Snapshot no existe")
    db.delete(s)
    db.commit()
    return {"ok": True}


# ============================================================
# Costos adicionales por producto (caja color, EXW->FOB, etc)
# ============================================================


class CostoAdicionalBody(BaseModel):
    concepto: str
    monto_usd: float
    notas: str | None = None


def _costo_to_dict(c: CostoAdicional) -> dict:
    return {
        "id": c.id,
        "producto_id": c.producto_id,
        "concepto": c.concepto,
        "monto_usd": c.monto_usd,
        "notas": c.notas,
        "creado_en": c.creado_en.isoformat() if c.creado_en else None,
    }


@router.get("/api/productos/{producto_id}/costos-adicionales")
def listar_costos(producto_id: int, db: SesionDep):
    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")
    costos = (
        db.query(CostoAdicional)
        .filter_by(producto_id=producto_id)
        .order_by(CostoAdicional.creado_en)
        .all()
    )
    total = sum(c.monto_usd for c in costos)
    return {
        "items": [_costo_to_dict(c) for c in costos],
        "total_usd": total,
        "fob_original": p.fob_usd,
        "fob_ajustado": (p.fob_usd or 0) + total,
    }


@router.post("/api/productos/{producto_id}/costos-adicionales")
def agregar_costo(producto_id: int, body: CostoAdicionalBody, db: SesionDep):
    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")
    if not body.concepto.strip():
        raise HTTPException(400, "Concepto requerido")
    if body.monto_usd <= 0:
        raise HTTPException(400, "monto_usd debe ser > 0")
    c = CostoAdicional(
        producto_id=producto_id,
        concepto=body.concepto.strip(),
        monto_usd=body.monto_usd,
        notas=(body.notas or None),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return _costo_to_dict(c)


@router.delete("/api/productos/{producto_id}/costos-adicionales/{costo_id}")
def borrar_costo(producto_id: int, costo_id: int, db: SesionDep):
    c = db.get(CostoAdicional, costo_id)
    if not c or c.producto_id != producto_id:
        raise HTTPException(404, "Costo no existe")
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.get("/api/cotizaciones")
def listar_cotizaciones_global(
    db: SesionDep,
    producto_id: int | None = Query(None),
    archivo: str | None = Query(None),
    origen: str | None = Query(None),
    limit: int = Query(500, le=2000),
):
    """Lista global de snapshots con joins a producto para mostrar SKU y descripcion.

    Filtros opcionales: producto_id, archivo (substring), origen.
    """
    q = (
        db.query(CotizacionSnapshot, Producto)
        .join(Producto, Producto.id == CotizacionSnapshot.producto_id)
    )
    if producto_id is not None:
        q = q.filter(CotizacionSnapshot.producto_id == producto_id)
    if archivo:
        q = q.filter(CotizacionSnapshot.archivo_exportado.ilike(f"%{archivo}%"))
    if origen:
        q = q.filter(CotizacionSnapshot.origen == origen)
    q = q.order_by(CotizacionSnapshot.creado_en.desc()).limit(limit)

    items = []
    for s, p in q.all():
        d = _snapshot_to_dict(s, p)
        d["sku"] = p.sku
        d["descripcion"] = p.descripcion
        d["categoria"] = p.categoria
        d["foto"] = p.fotos[0].ruta_relativa if p.fotos else None
        items.append(d)
    return {"total": len(items), "items": items}


@router.get("/cotizaciones", response_class=HTMLResponse)
def pagina_cotizaciones(request: Request, db: SesionDep):
    """Pagina con historial global de cotizaciones (todos los snapshots)."""
    return TEMPLATES.TemplateResponse(
        request,
        "cotizaciones.html",
        {},
    )


@router.get("/aranceles", response_class=HTMLResponse)
def pagina_aranceles(request: Request, db: SesionDep):
    """Pagina dedicada para CRUD de overrides de aranceles."""
    cats = (
        db.query(Producto.categoria)
        .distinct()
        .all()
    )
    categorias = sorted(
        [c for (c,) in cats if c and c != "_descartar"]
    )
    return TEMPLATES.TemplateResponse(
        request,
        "aranceles.html",
        {"categorias_disponibles": categorias},
    )


# ============================================================
# Catalogo de categorias (CRUD + patrones de descarte)
# ============================================================
#
# BD es la fuente de verdad para el clasificador en runtime. El YAML
# config/categorias.yml es solo seed inicial: las ediciones via UI van
# directo a BD y NO se sincronizan al YAML. Tras cualquier mutacion se
# invalida el cache del clasificador.


import re as _re


SLUG_VALIDO = _re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class CategoriaBody(BaseModel):
    slug: str
    orden: int = 100


class KeywordsBody(BaseModel):
    keywords: list[str]


class PatronDescarteBody(BaseModel):
    patron: str
    nota: str | None = None


def _cat_a_dict(c: Categoria, conteo_productos: int) -> dict:
    return {
        "id": c.id,
        "slug": c.slug,
        "orden": c.orden,
        "keywords": sorted(kw.keyword for kw in c.keywords),
        "productos": conteo_productos,
    }


def _validar_slug(slug: str):
    slug = (slug or "").strip().lower()
    if not slug:
        raise HTTPException(422, "slug vacio")
    if slug == "_descartar":
        raise HTTPException(422, "'_descartar' es reservada por el sistema")
    if not SLUG_VALIDO.match(slug):
        raise HTTPException(
            422,
            "slug invalido: usa minusculas, numeros, guion o guion-bajo "
            "(ej. 'casa-jaula'). Sin espacios ni acentos.",
        )
    return slug


@router.get("/api/catalogo/categorias")
def catalogo_listar(db: SesionDep):
    """Lista categorias del catalogo con keywords y conteo de productos."""
    conteos = dict(
        db.query(Producto.categoria, func.count(Producto.id))
        .filter(Producto.categoria.isnot(None))
        .group_by(Producto.categoria)
        .all()
    )
    items = []
    for c in (
        db.query(Categoria)
        .order_by(Categoria.orden.asc(), Categoria.slug.asc())
        .all()
    ):
        items.append(_cat_a_dict(c, int(conteos.get(c.slug, 0))))
    return {"items": items}


@router.post("/api/catalogo/categorias")
def catalogo_crear(body: CategoriaBody, db: SesionDep):
    slug = _validar_slug(body.slug)
    if db.query(Categoria).filter_by(slug=slug).first():
        raise HTTPException(409, f"Ya existe una categoria con slug '{slug}'")
    c = Categoria(slug=slug, orden=body.orden)
    db.add(c)
    db.commit()
    db.refresh(c)
    invalidar_cache()
    return _cat_a_dict(c, 0)


@router.patch("/api/catalogo/categorias/{cat_id}")
def catalogo_editar(cat_id: int, body: CategoriaBody, db: SesionDep):
    c = db.get(Categoria, cat_id)
    if not c:
        raise HTTPException(404, "Categoria no existe")
    slug_nuevo = _validar_slug(body.slug)
    if slug_nuevo != c.slug:
        if db.query(Categoria).filter_by(slug=slug_nuevo).first():
            raise HTTPException(409, f"Ya existe '{slug_nuevo}'")
        # Renombrar el valor en productos.categoria para no orfanar nada
        db.query(Producto).filter(Producto.categoria == c.slug).update(
            {"categoria": slug_nuevo}, synchronize_session=False
        )
        c.slug = slug_nuevo
    c.orden = body.orden
    db.commit()
    n = (
        db.query(func.count(Producto.id))
        .filter(Producto.categoria == c.slug)
        .scalar()
    )
    invalidar_cache()
    return _cat_a_dict(c, int(n or 0))


@router.delete("/api/catalogo/categorias/{cat_id}")
def catalogo_eliminar(cat_id: int, db: SesionDep):
    c = db.get(Categoria, cat_id)
    if not c:
        raise HTTPException(404, "Categoria no existe")
    n = (
        db.query(func.count(Producto.id))
        .filter(Producto.categoria == c.slug)
        .scalar()
    )
    if n:
        raise HTTPException(
            409,
            f"No se puede borrar: {int(n)} productos siguen asignados a "
            f"'{c.slug}'. Reasignalos antes.",
        )
    db.delete(c)
    db.commit()
    invalidar_cache()
    return {"ok": True}


@router.put("/api/catalogo/categorias/{cat_id}/keywords")
def catalogo_keywords(cat_id: int, body: KeywordsBody, db: SesionDep):
    """Reemplaza la lista completa de keywords de la categoria."""
    c = db.get(Categoria, cat_id)
    if not c:
        raise HTTPException(404, "Categoria no existe")
    # Normalizar: lowercase, sin espacios al borde, sin vacios, sin duplicados
    limpias = []
    vistas = set()
    for kw in body.keywords:
        k = (kw or "").strip().lower()
        if not k or k in vistas:
            continue
        vistas.add(k)
        limpias.append(k)
    db.query(CategoriaKeyword).filter_by(categoria_id=c.id).delete()
    for k in limpias:
        db.add(CategoriaKeyword(categoria_id=c.id, keyword=k))
    db.commit()
    invalidar_cache()
    n = (
        db.query(func.count(Producto.id))
        .filter(Producto.categoria == c.slug)
        .scalar()
    )
    db.refresh(c)
    return _cat_a_dict(c, int(n or 0))


@router.get("/api/catalogo/patrones-descarte")
def patrones_listar(db: SesionDep):
    rows = db.query(PatronDescarte).order_by(PatronDescarte.id.asc()).all()
    return {
        "items": [
            {"id": p.id, "patron": p.patron, "nota": p.nota} for p in rows
        ]
    }


@router.post("/api/catalogo/patrones-descarte")
def patrones_crear(body: PatronDescarteBody, db: SesionDep):
    patron = (body.patron or "").strip()
    if not patron:
        raise HTTPException(422, "patron vacio")
    try:
        _re.compile(patron, _re.IGNORECASE)
    except _re.error as e:
        raise HTTPException(422, f"regex invalido: {e}")
    if db.query(PatronDescarte).filter_by(patron=patron).first():
        raise HTTPException(409, "Ese patron ya existe")
    p = PatronDescarte(patron=patron, nota=(body.nota or "").strip() or None)
    db.add(p)
    db.commit()
    db.refresh(p)
    invalidar_cache()
    return {"id": p.id, "patron": p.patron, "nota": p.nota}


@router.delete("/api/catalogo/patrones-descarte/{pat_id}")
def patrones_eliminar(pat_id: int, db: SesionDep):
    p = db.get(PatronDescarte, pat_id)
    if not p:
        raise HTTPException(404, "Patron no existe")
    db.delete(p)
    db.commit()
    invalidar_cache()
    return {"ok": True}


@router.post("/api/catalogo/reclasificar-sin-categoria")
def reclasificar_sin_categoria(db: SesionDep):
    """Aplica el clasificador a productos con categoria IS NULL.

    No toca productos ya clasificados. Devuelve {asignados, sin_match}.
    """
    invalidar_cache()
    asignados = 0
    sin_match = 0
    for p in db.query(Producto).filter(Producto.categoria.is_(None)).all():
        cat = clasificar_descripcion(p.descripcion)
        if cat is None:
            sin_match += 1
        else:
            p.categoria = cat
            asignados += 1
    db.commit()
    return {"asignados": asignados, "sin_match": sin_match}


@router.get("/categorias", response_class=HTMLResponse)
def pagina_categorias(request: Request, db: SesionDep):
    """Pagina CRUD del catalogo de categorias y patrones de descarte."""
    sin_categoria = (
        db.query(func.count(Producto.id))
        .filter(Producto.categoria.is_(None))
        .scalar()
    )
    return TEMPLATES.TemplateResponse(
        request,
        "categorias.html",
        {"productos_sin_categoria": int(sin_categoria or 0)},
    )


# ============================================================
# Usuarios autorizados (whitelist Google OAuth)
# ============================================================

class UsuarioCreateBody(BaseModel):
    email: str
    nombre: str | None = None


class UsuarioPatchBody(BaseModel):
    nombre: str | None = None
    activo: bool | None = None


def _usuario_to_dict(u: UsuarioAutorizado) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "nombre": u.nombre,
        "activo": u.activo,
        "creado_en": u.creado_en.isoformat() if u.creado_en else None,
        "ultimo_login": u.ultimo_login.isoformat() if u.ultimo_login else None,
    }


def _email_session(request: Request) -> str | None:
    """Email del usuario actualmente logueado, si lo hay (None en tests)."""
    return (request.session.get("user") or {}).get("email")


@router.get("/api/usuarios")
def listar_usuarios(db: SesionDep):
    items = (
        db.query(UsuarioAutorizado)
        .order_by(UsuarioAutorizado.activo.desc(), UsuarioAutorizado.email)
        .all()
    )
    return {"items": [_usuario_to_dict(u) for u in items]}


@router.post("/api/usuarios")
def crear_usuario(body: UsuarioCreateBody, db: SesionDep):
    email = (body.email or "").strip().lower()
    if not email or "@" not in email or "." not in email.split("@", 1)[1]:
        raise HTTPException(400, "Email invalido")
    if db.query(UsuarioAutorizado).filter(UsuarioAutorizado.email == email).first():
        raise HTTPException(409, "Ese correo ya esta autorizado")
    u = UsuarioAutorizado(
        email=email,
        nombre=((body.nombre or "").strip() or None),
        activo=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return _usuario_to_dict(u)


@router.patch("/api/usuarios/{usuario_id}")
def editar_usuario(
    usuario_id: int, body: UsuarioPatchBody, request: Request, db: SesionDep
):
    u = db.get(UsuarioAutorizado, usuario_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")

    if body.nombre is not None:
        u.nombre = body.nombre.strip() or None

    if body.activo is not None and body.activo != u.activo:
        actual_email = _email_session(request)
        # Guardia: no desactivarse a si mismo.
        if not body.activo and actual_email and actual_email == u.email:
            raise HTTPException(400, "No puedes desactivarte a ti mismo")
        # Guardia: no dejar a la app sin ningun usuario activo.
        if not body.activo:
            activos = (
                db.query(UsuarioAutorizado)
                .filter(UsuarioAutorizado.activo == True)  # noqa: E712
                .count()
            )
            if activos <= 1:
                raise HTTPException(400, "No puedes desactivar al ultimo usuario activo")
        u.activo = body.activo

    db.commit()
    db.refresh(u)
    return _usuario_to_dict(u)


@router.delete("/api/usuarios/{usuario_id}")
def borrar_usuario(usuario_id: int, request: Request, db: SesionDep):
    u = db.get(UsuarioAutorizado, usuario_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")
    actual_email = _email_session(request)
    if actual_email and actual_email == u.email:
        raise HTTPException(400, "No puedes borrarte a ti mismo")
    if u.activo:
        activos = (
            db.query(UsuarioAutorizado)
            .filter(UsuarioAutorizado.activo == True)  # noqa: E712
            .count()
        )
        if activos <= 1:
            raise HTTPException(400, "No puedes borrar al ultimo usuario activo")
    db.delete(u)
    db.commit()
    return {"ok": True}


@router.get("/usuarios", response_class=HTMLResponse)
def pagina_usuarios(request: Request, db: SesionDep):
    return TEMPLATES.TemplateResponse(request, "usuarios.html", {})
