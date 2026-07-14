import threading
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, Integer, case, cast
from sqlalchemy.orm import Session

from app import db as db_module
from app import help_content
from app import jobs
from app.modelos import (
    Producto, Proveedor, ArancelOverride, Arancel, CostoAdicional,
    CotizacionSnapshot, Foto, Categoria, CategoriaKeyword, PatronDescarte,
    UsuarioAutorizado, CompetidorListing,
)
from app.clasificador import clasificar_descripcion, invalidar_cache
from app.ingest import (
    buscar_proveedor_existente,
    cbm_desde_carton_dims,
    cbm_es_discrepante,
    pzas_40hq_desde_cbm_y_caja,
    pzas_caja_desde_nw_y_peso,
)

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SIN_CATEGORIA = "__sin_categoria__"  # sentinela en query string

FAVICON_PATH = Path(__file__).parent / "favicon.ico"


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(FAVICON_PATH)


@router.get("/fotos/{ruta:path}", include_in_schema=False)
def servir_foto(ruta: str, request: Request):
    """Sirve fotos del proyecto activo (reemplaza el StaticFiles global).

    Publica (en PUBLIC_PREFIXES) pero resuelve la carpeta desde
    session['proyecto']: las fotos las pide el navegador como subrecurso de una
    pagina autenticada, asi que la cookie de sesion trae el proyecto. Guard
    anti path-traversal: el archivo debe quedar dentro de la carpeta del proyecto.
    """
    slug = request.session.get("proyecto")
    if not slug or not db_module.slug_valido(slug):
        raise HTTPException(404)
    fotos_dir = db_module.fotos_dir_proyecto(slug).resolve()
    destino = (fotos_dir / ruta).resolve()
    if not destino.is_file() or not destino.is_relative_to(fotos_dir):
        raise HTTPException(404)
    return FileResponse(str(destino))


def get_db(request: Request):
    # Lookup tardio para permitir monkeypatch en tests. El proyecto activo se
    # resuelve desde session['proyecto']; en tests get_db se sustituye entero via
    # app.dependency_overrides, asi que la firma con Request no los afecta.
    slug = request.session.get("proyecto")
    SessionFactory = db_module.get_session_factory(slug)
    db = SessionFactory()
    try:
        yield db
    finally:
        db.close()


SesionDep = Annotated[Session, Depends(get_db)]


def _slug_activo(request: Request) -> str:
    """Slug del proyecto activo en la sesion. 400 si no hay ninguno.

    El ProyectoMiddleware ya redirige/rechaza requests sin proyecto, pero los
    endpoints que construyen rutas de fotos/exports lo revalidan por defensa."""
    slug = request.session.get("proyecto")
    if not slug:
        raise HTTPException(400, "No hay proyecto seleccionado.")
    return slug


def _base_fotos(slug: str) -> str:
    """Carpeta que contiene 'fotos/' del proyecto (ruta_relativa = 'fotos/<n>')."""
    return str(db_module.fotos_dir_proyecto(slug).parent)


def get_sistema_db():
    """Sesion de la BD de sistema (usuarios_autorizados, proyectos). Global,
    independiente del proyecto activo."""
    SessionFactory = db_module.get_sistema_session_factory()
    db = SessionFactory()
    try:
        yield db
    finally:
        db.close()


SistemaDep = Annotated[Session, Depends(get_sistema_db)]


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


class RenombrarProveedorBody(BaseModel):
    nombre: str


class MarcarBulkBody(BaseModel):
    ids: list[int]
    marcado: bool


class DescribirFotosBody(BaseModel):
    # forzar=True: redescribe TAMBIEN los que ya tienen descripcion (para
    # arreglar descripciones malas). proveedor_id: acota a un proveedor.
    forzar: bool = False
    proveedor_id: int | None = None


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
                "pzas_40hq": p.pzas_40hq,
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


def _worker_ingest_pdf(
    job_id: str,
    destino: Path,
    fotos_destino: Path,
    forzar: bool,
    slug: str,
    proveedor_forzado: str | None = None,
) -> None:
    """Corre la extraccion+ingest del PDF en un hilo aparte y guarda el
    resultado en el registro de jobs.

    Mapea el desenlace al mismo (http_status, body) que devolvia el endpoint
    sincrono, para que el front aplique exactamente la misma logica:
      - 200 + {ok:True}        -> exito
      - 422 + {ok:False,...}   -> gate (VACIO / REINGESTAR)
      - 500 + {detail:...}     -> fallo del pipeline o error inesperado
    """
    from app.pdf_pipeline import procesar_pdf, PdfPipelineError, limpiar_artefactos

    # Sesion nueva propia del hilo, ligada a la BD del proyecto `slug` capturado
    # al encolar (el worker no tiene request/sesion). procesar_pdf hace
    # commit/rollback adentro.
    SessionFactory = db_module.get_session_factory(slug)
    try:
        with SessionFactory() as session:
            try:
                resultado = procesar_pdf(
                    session=session,
                    pdf_path=destino,
                    fotos_destino=fotos_destino,
                    forzar_calidad=forzar,
                    proveedor_forzado=proveedor_forzado,
                )
            except PdfPipelineError as e:
                limpiar_artefactos(destino, borrar_pdf=True)
                jobs.marcar_listo(job_id, 500, {"detail": str(e)})
                return
            http_status = 200 if resultado.get("ok") else 422
            # Vision automatica: si el ingest fue OK, describir por imagen los
            # productos del proveedor recien ingestado que quedaron sin descripcion
            # real (vacia o placeholder 'Product N'). No falla el ingest si la
            # vision falla; solo se anota en el resultado.
            if resultado.get("ok"):
                try:
                    prov = buscar_proveedor_existente(session, resultado.get("proveedor") or "")
                    if prov is not None:
                        vis = _describir_sin_descripcion(session, slug, proveedor_id=prov.id)
                        resultado["descripciones_ia"] = {
                            "descritos": vis["descritos"],
                            "clasificados": vis.get("clasificados", 0),
                            "sin_foto": vis["sin_foto"],
                            "total": vis["total"],
                            "aviso": vis.get("aviso"),
                            "error": vis.get("error"),
                        }
                except Exception as e:
                    resultado["descripciones_ia"] = {"error": f"vision fallo: {e}"}
            # Auto-limpieza de artefactos de trabajo (extract + intermedio). En
            # exito se conserva el PDF (referenciado como "Cotizacion original");
            # en fallo del gate se borra tambien el PDF (nada quedo en BD).
            limpiar_artefactos(destino, borrar_pdf=(http_status != 200))
            jobs.marcar_listo(job_id, http_status, resultado)
    except Exception as e:  # red de seguridad: nunca dejar el job colgado
        limpiar_artefactos(destino, borrar_pdf=True)
        jobs.marcar_listo(job_id, 500, {"detail": f"Error inesperado: {e}"})


@router.post("/api/ingest/pdf", status_code=202)
async def ingestar_pdf(
    request: Request,
    file: UploadFile = File(...),
    forzar: bool = Form(False),
    proveedor_id: int | None = Form(None),
):
    """Sube un PDF de cotizacion y arranca su ingest en segundo plano.

    Responde al instante (202) con un job_id; el procesamiento pesado
    (pdf_a_formato_hd.py -> ingest -> gate de calidad) corre en un hilo aparte
    para no toparse con el timeout de ~100s de Cloudflare (524). El cliente
    consulta el avance en GET /api/ingest/pdf/status/{job_id}.

    Validaciones rapidas (extension/tamano) y el guardado del PDF siguen siendo
    sincronos: si fallan, devuelve 4xx aqui mismo.
    """
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

    slug = _slug_activo(request)

    # Proveedor destino opcional: si se eligio uno, sus productos se agregan a
    # ese proveedor (util cuando una propuesta viene partida en varios PDFs).
    proveedor_forzado = None
    if proveedor_id is not None:
        SessionFactory = db_module.get_session_factory(slug)
        with SessionFactory() as s:
            pv = s.get(Proveedor, proveedor_id)
            if pv is None:
                raise HTTPException(404, f"Proveedor {proveedor_id} no existe en este proyecto")
            proveedor_forzado = pv.nombre

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

    fotos_destino = db_module.fotos_dir_proyecto(slug)
    fotos_destino.mkdir(parents=True, exist_ok=True)

    # Lanza el procesamiento en background y devuelve el job_id de inmediato.
    job_id = jobs.crear_job(base)
    hilo = threading.Thread(
        target=_worker_ingest_pdf,
        args=(job_id, destino, fotos_destino, forzar, slug, proveedor_forzado),
        daemon=True,
    )
    hilo.start()
    return {"job_id": job_id, "estado": "procesando", "archivo": base}


@router.get("/api/ingest/pdf/status/{job_id}")
async def estado_ingest_pdf(job_id: str):
    """Devuelve el estado de un job de ingest.

    - 200 {estado:"procesando", elapsed:<seg>}  mientras corre.
    - 200 {estado:"listo", http_status:<200|422|500>, resultado:{...}, elapsed}
      cuando termina. El front aplica su logica usando http_status + resultado.
    - 404 si el job_id no existe (p.ej. la app se reinicio).
    """
    job = jobs.obtener_job(job_id)
    if job is None:
        raise HTTPException(404, "Job no encontrado (puede que el servidor se haya reiniciado).")
    return job


@router.post("/api/productos/{producto_id}/foto")
async def subir_foto(
    producto_id: int, db: SesionDep, request: Request, file: UploadFile = File(...)
):
    """Sube o reemplaza la foto principal del producto.

    Si ya existia una foto principal: borra el archivo del disco y la fila Foto.
    Guarda la nueva en la carpeta de fotos del proyecto activo con nombre
    derivado del producto + epoch.
    """
    slug = _slug_activo(request)
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

    fotos_dir = db_module.fotos_dir_proyecto(slug)
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
        # rel viene como "fotos/<nombre>"; resolvemos contra la carpeta del proyecto
        if rel.startswith("fotos/"):
            archivo_viejo = fotos_dir.parent / rel
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


def _fusionar_proveedores(db: Session, origen: Proveedor, destino: Proveedor):
    """Reasigna los productos de `origen` a `destino` y elimina `origen`.

    El UniqueConstraint(proveedor_id, sku) impide mover un producto a `destino`
    si este ya tiene uno con el mismo sku. Esos duplicados se descartan del
    origen (reutilizando _borrar_productos_por_ids). Productos sin sku nunca
    chocan y siempre se mueven. Devuelve (movidos, descartados).
    """
    sk_destino = {
        sku for (sku,) in db.query(Producto.sku)
        .filter(Producto.proveedor_id == destino.id).all()
        if sku
    }
    movidos, a_descartar = 0, []
    for p in db.query(Producto).filter(Producto.proveedor_id == origen.id).all():
        if p.sku and p.sku in sk_destino:
            a_descartar.append(p.id)          # duplicado de SKU -> se descarta
        else:
            p.proveedor_id = destino.id
            if p.sku:
                sk_destino.add(p.sku)
            movidos += 1
    descartados = _borrar_productos_por_ids(db, a_descartar) if a_descartar else 0
    # Preservar la referencia al PDF original: si el destino no tiene archivo_pdf,
    # hereda el del origen para que los productos movidos no pierdan su
    # "Cotizacion original" al fusionar.
    if not destino.archivo_pdf and origen.archivo_pdf:
        destino.archivo_pdf = origen.archivo_pdf
    db.flush()
    db.delete(origen)                          # proveedor origen ya vacio
    return movidos, descartados


@router.patch("/api/proveedores/{proveedor_id}")
def renombrar_proveedor(
    proveedor_id: int, body: RenombrarProveedorBody, db: SesionDep
):
    """Renombra un proveedor. Si el nuevo nombre matchea (matching tolerante)
    a OTRO proveedor existente, fusiona: mueve los productos al existente y
    elimina el duplicado."""
    pv = db.get(Proveedor, proveedor_id)
    if not pv:
        raise HTTPException(404, "Proveedor no existe")
    nuevo = (body.nombre or "").strip()[:200]
    if not nuevo:
        raise HTTPException(400, "Nombre vacio")

    destino = buscar_proveedor_existente(db, nuevo)
    if destino and destino.id != pv.id:
        movidos, descartados = _fusionar_proveedores(db, origen=pv, destino=destino)
        db.commit()
        return {
            "ok": True, "fusionado": True,
            "proveedor_id": destino.id, "nombre": destino.nombre,
            "movidos": movidos, "descartados": descartados,
        }

    pv.nombre = nuevo
    db.commit()
    return {
        "ok": True, "fusionado": False,
        "proveedor_id": pv.id, "nombre": pv.nombre,
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
    # Preferir el PDF de origen del PRODUCTO (soporta 1 proveedor con varios
    # PDFs); fallback al del proveedor (legacy / productos previos a la columna).
    archivo = (p.archivo_pdf or "") or ((p.proveedor.archivo_pdf or "") if p.proveedor else "")
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

    archivo = (p.archivo_pdf or "") or ((p.proveedor.archivo_pdf or "") if p.proveedor else "")
    if not archivo:
        return {"existe": False, "razon": "Producto/proveedor sin archivo_pdf asociado"}

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

    # Estado del arancel de la categoria del producto (para badge en el front:
    # 'pendiente' = categoria sin fraccion confirmada -> la cotizacion usa default).
    cat_arancel_estado = None
    if p.categoria:
        _cat = db.query(Categoria).filter_by(slug=p.categoria).first()
        if _cat is not None:
            cat_arancel_estado = _cat.arancel_estado

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
    # (editar retail -> calcular margen efectivo). Pasamos settings para que
    # iva/descuentos reflejen los overrides de la barra (mismos valores que uso
    # el motor en _calc_from_paso9), no los defaults.
    from app.cotizador.defaults import country_params, tariff_params, DEFAULTS
    cp = country_params(res.country_code, settings=settings)
    total_desc_pct = float(cp["descuentos_pct"]) + float(cp["descuentos_na_pct"]) + float(cp["gasto_fijo_pct"])

    # Valores conocidos que entraron a cada paso, para mostrarlos entre
    # parentesis junto al label (ej. "x piezas por contenedor (150 pzas)").
    # piezas se deriva del propio resultado (paso3 = paso2 x piezas) para
    # reflejar tanto el override como el valor derivado de carton_qty + cbm.
    def _g(x):  # 25.0 -> "25", 1.15 -> "1.15", sin ceros sobrantes
        return f"{float(x):g}"

    mult = tariff_params(res.tasa_arancelaria_pct)["multiplicador_arancelario"]
    piezas_usadas = (
        int((res.paso3 / res.paso2).to_integral_value())
        if res.paso2 and res.paso2 != 0 else None
    )
    _pzas = f"{piezas_usadas:,}" if piezas_usadas else ""
    flete_mar = flete_maritimo_usd if flete_maritimo_usd is not None else DEFAULTS["flete_maritimo_usd"]
    ga_pct = settings.get("gastos_aduanales_pct", DEFAULTS["gastos_aduanales_pct"])
    paso_notas = {
        2: f"{float(mult):.2f}",
        3: f"{_pzas} pzas" if _pzas else "",
        4: f"${float(flete_mar):,.0f}",
        5: f"{_g(res.tasa_arancelaria_pct)}% + DTA {_g(DEFAULTS['dta_pct'])}%",
        6: f"{_g(ga_pct)}%",
        7: f"{float(res.tipo_cambio):.2f}",
        8: f"${float(settings['flete_local_mxn']):,.0f}",
        9: f"÷ {_pzas} pzas" if _pzas else "",
        10: f"{_g(total_desc_pct)}%",
        11: f"{_g(res.margen_nuestro_effective)}%",
        12: f"{_g(res.margen_cliente_effective)}%",
        13: f"IVA {_g(cp['iva_pct'])}%",
    }
    if suma_costos > 0:
        paso_notas[1] = f"FOB ${float(fob_original):,.2f} + costos ${float(suma_costos):,.2f}"

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
        "categoria_arancel_estado": cat_arancel_estado,
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
                "nota": paso_notas.get(i, ""),
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
    base_fotos: str,
    categoria: str | None = "__usar_marcados__",
    params: dict | None = None,
) -> Path:
    """Genera intermedio (por marcas o por categoria) + corre llenar_formato_hd.py.

    Si categoria == '__usar_marcados__' (default): filtra por marcado_cotizar=True.
    Si categoria es None: filtra productos sin categoria.
    Si categoria es str: filtra por esa categoria.

    `base_fotos` es la carpeta del proyecto que contiene 'fotos/' (para resolver
    las imagenes en el xlsx). `xlsx_int`/`salida` viven en la carpeta de exports
    del proyecto. `proyecto` (raiz del repo) solo se usa para scripts/formato/cwd.

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
            base_fotos=base_fotos,
            params=params,
        )
        if n == 0:
            raise HTTPException(400, "No hay productos marcados.")
    else:
        n = generar_formato_hd_por_categoria(
            session=db,
            xlsx_intermedio=str(xlsx_int),
            base_fotos=base_fotos,
            categoria=categoria,
            params=params,
        )
        if n == 0:
            raise HTTPException(404, f"No hay productos en categoria {categoria!r}.")

    formato_env = os.environ.get("FORMATO_HD_PATH")
    formato = Path(formato_env) if formato_env else proyecto / "Pet Quote Sheet 2026.xlsb"
    # FORMATO_HD_PATH puede apuntar a una ruta del host (ej. /home/salomon/...)
    # que no existe dentro del contenedor (el proyecto se monta en /app). Si la
    # ruta configurada no existe, caer al template del proyecto por su nombre.
    if not formato.exists():
        alterno = proyecto / formato.name
        if alterno.exists():
            formato = alterno

    # Mapeo intermedio (18 cols) -> filas del HD destino. Fuente unica para ambas
    # rutas (Windows COM / Linux openpyxl):
    #   col B (SKU)->fila 5, C (Desc)->8, H (MOQ)->12, M (Pzas 40hq)->15,
    #   O (Venta HD)->11, P (Retail MXN)->16, Q (Margen)->17.
    # Fila 4 (Vendor Number) queda "TBD" via CONSTANTES; el proveedor (R) no se mapea.
    MAPEO_HD = "B=5,C=8,H=12,M=15,O=11,P=16,Q=17"

    if _sys.platform == "win32":
        # --- Windows: Excel via COM (llenar_formato_hd.py) ---
        script = proyecto / "llenar_formato_hd.py"
        # Replicar el naming de llenar_formato_hd.construir_nombre_salida().
        base_salida = xlsx_int.stem.lower()
        if base_salida.startswith("_intermedio_"):
            base_salida = base_salida[len("_intermedio_"):]
        salida_default = proyecto / f"formato-hd-{base_salida}.xlsx"
        for p in {salida, salida_default}:
            if p.exists():
                try:
                    p.unlink()
                except PermissionError:
                    raise HTTPException(
                        500,
                        f"No puedo borrar el archivo anterior (esta abierto en Excel?): {p.name}",
                    )
        result = subprocess.run(
            [_sys.executable, str(script), str(xlsx_int), str(formato),
             "--mapeo", MAPEO_HD, "--yes"],
            capture_output=True, text=True, cwd=str(proyecto),
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise HTTPException(500, f"Fallo llenar_formato_hd: {result.stderr[:500]}")
        if not salida_default.exists():
            raise HTTPException(500, f"No encontre el archivo de salida: {salida_default.name}")
        if salida_default != salida:
            salida_default.replace(salida)
        return salida

    # --- Linux/servidor: openpyxl (sin Excel). openpyxl no lee .xlsb, asi que
    # el template se convierte a .xlsx con LibreOffice (asegurar_template_xlsx). ---
    from app import formato_hd
    if salida.exists():
        try:
            salida.unlink()
        except PermissionError:
            raise HTTPException(500, f"No puedo borrar el archivo anterior: {salida.name}")
    try:
        template_xlsx = formato_hd.asegurar_template_xlsx(formato)
    except formato_hd.LibreOfficeNoDisponible as e:
        raise HTTPException(
            500,
            "Falta LibreOffice en el servidor para convertir el template HD (.xlsb) "
            f"a .xlsx. Instalalo o provee un template .xlsx en FORMATO_HD_PATH. ({e})",
        )
    except Exception as e:
        raise HTTPException(500, f"No pude preparar el template HD: {e}")
    try:
        formato_hd.llenar_formato_hd(
            str(xlsx_int), str(template_xlsx), str(salida),
            formato_hd.parsear_mapeo(MAPEO_HD),
        )
    except Exception as e:
        raise HTTPException(500, f"Fallo el llenado del formato HD: {e}")
    if not salida.exists():
        raise HTTPException(500, f"No se genero el archivo de salida: {salida.name}")
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
    request: Request,
    params: dict = Depends(_params_exportar),
):
    """Genera HD desde la seleccion actual (compatibilidad)."""
    slug = _slug_activo(request)
    exports = db_module.exports_dir_proyecto(slug)
    exports.mkdir(parents=True, exist_ok=True)
    xlsx_int = exports / "_intermedio_seleccion.xlsx"
    salida = exports / f"formato-hd-{xlsx_int.stem.lower()}.xlsx"
    archivo = _correr_llenar_formato_hd(
        db, xlsx_int, salida, _base_fotos(slug), params=params
    )
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
    request: Request,
    params: dict = Depends(_params_exportar),
):
    """Genera xlsx vertical con TODAS las columnas (foto, FOB, costos, arancel,
    landing, venta HD, retail, margenes) de los productos marcados. Uso interno.
    """
    from app.exportar import generar_export_interno_marcados

    slug = _slug_activo(request)
    exports = db_module.exports_dir_proyecto(slug)
    exports.mkdir(parents=True, exist_ok=True)
    fecha = date.today().strftime("%Y%m%d")
    salida = exports / f"cotizacion-interna-{fecha}.xlsx"
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
        base_fotos=_base_fotos(slug),
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
    request: Request,
    params: dict = Depends(_params_exportar),
):
    """Genera HD para una categoria sin tocar el estado de marcas.

    Si categoria == '__sin_categoria__', exporta productos sin categoria.
    """
    # Validar que la categoria existe (con productos) ANTES de resolver el
    # proyecto: un 404 de categoria no debe quedar tapado por el 400 de proyecto.
    q = _aplicar_filtro_categoria(db.query(Producto), categoria)
    n_en_cat = q.count()
    if n_en_cat == 0:
        raise HTTPException(404, f"Categoria '{categoria}' no tiene productos.")

    slug = _slug_activo(request)
    exports = db_module.exports_dir_proyecto(slug)
    exports.mkdir(parents=True, exist_ok=True)

    # Nombre con fecha del dia (no del correo origen, como dice el plan)
    cat_slug = "sin-categoria" if categoria == SIN_CATEGORIA else categoria
    fecha = date.today().strftime("%Y%m%d")
    xlsx_int = exports / f"_intermedio_{cat_slug}-{fecha}.xlsx"
    salida = exports / f"formato-hd-{cat_slug}-{fecha}.xlsx"

    # Pasamos categoria=None si el cliente uso el sentinela __sin_categoria__
    cat_filter = None if categoria == SIN_CATEGORIA else categoria
    archivo = _correr_llenar_formato_hd(
        db, xlsx_int, salida, _base_fotos(slug), categoria=cat_filter, params=params
    )
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
def exportar_pd(db: SesionDep, request: Request):
    """Genera el formato Pet PD (Product Dimension) con productos marcados.

    No usa el motor de cotizacion (no toma TC/margenes/etc): solo datos
    fisicos del producto. Por eso no recibe params.
    """
    from app.exportar import generar_export_pd_desde_marcados

    slug = _slug_activo(request)
    exports = db_module.exports_dir_proyecto(slug)
    exports.mkdir(parents=True, exist_ok=True)
    fecha = date.today().strftime("%Y%m%d")
    salida = exports / f"export-pd-marcados-{fecha}.xlsx"
    if salida.exists():
        try:
            salida.unlink()
        except PermissionError:
            raise HTTPException(
                500,
                f"No puedo borrar el archivo anterior (esta abierto en Excel?): {salida.name}",
            )
    n = generar_export_pd_desde_marcados(db, str(salida), _base_fotos(slug))
    if n == 0:
        raise HTTPException(400, "No hay productos marcados.")
    return FileResponse(
        str(salida),
        filename=salida.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/exportar-pd/{categoria}")
def exportar_pd_categoria(categoria: str, db: SesionDep, request: Request):
    """Genera Pet PD para una categoria sin tocar marcas.

    Si categoria == '__sin_categoria__', exporta productos sin categoria.
    """
    from app.exportar import generar_export_pd_por_categoria

    slug = _slug_activo(request)
    exports = db_module.exports_dir_proyecto(slug)
    exports.mkdir(parents=True, exist_ok=True)
    cat_filter = None if categoria == SIN_CATEGORIA else categoria
    cat_slug = "sin-categoria" if cat_filter is None else categoria
    fecha = date.today().strftime("%Y%m%d")
    salida = exports / f"export-pd-{cat_slug}-{fecha}.xlsx"
    if salida.exists():
        try:
            salida.unlink()
        except PermissionError:
            raise HTTPException(
                500,
                f"No puedo borrar el archivo anterior (esta abierto en Excel?): {salida.name}",
            )
    n = generar_export_pd_por_categoria(db, str(salida), _base_fotos(slug), cat_filter)
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


# ============================================================
# Competencia: busqueda en Amazon MX / Mercado Libre MX (Claude web_search)
# ============================================================


class CompetenciaBuscarBody(BaseModel):
    query: str | None = None
    # Sitio(s) extra a agregar a esta busqueda (dominios o URLs, separados por
    # coma/espacio). Se SUMAN a los sitios configurados en la categoria.
    sitios_extra: str | None = None


class CompetidorItem(BaseModel):
    marketplace: str
    titulo: str
    precio_mxn: float | None = None
    rating: float | None = None
    num_reviews: int | None = None
    vendedor: str | None = None
    url: str
    imagen_url: str | None = None
    notas: str | None = None


class CompetenciaGuardarBody(BaseModel):
    items: list[CompetidorItem]
    query: str | None = None


def _competidor_to_dict(c: CompetidorListing) -> dict:
    return {
        "id": c.id,
        "producto_id": c.producto_id,
        "marketplace": c.marketplace,
        "titulo": c.titulo,
        "precio_mxn": c.precio_mxn,
        "rating": c.rating,
        "num_reviews": c.num_reviews,
        "vendedor": c.vendedor,
        "url": c.url,
        "imagen_url": c.imagen_url,
        "busqueda_query": c.busqueda_query,
        "notas": c.notas,
        "creado_en": c.creado_en.isoformat() if c.creado_en else None,
    }


def _stats_precios(items: list[CompetidorListing]) -> dict | None:
    precios = [c.precio_mxn for c in items if c.precio_mxn and c.precio_mxn > 0]
    if not precios:
        return {"n": len(items), "min": None, "prom": None, "max": None}
    return {
        "n": len(items),
        "min": min(precios),
        "prom": sum(precios) / len(precios),
        "max": max(precios),
    }


# Marketplaces canonicos (van primero en el resumen); el resto son sitios extra.
_MP_CANONICOS = ("amazon_mx", "mercadolibre_mx", "petco_mx")


def _resumen_competencia(items: list[CompetidorListing]) -> dict:
    """Stats de precio por marketplace presente + global. Los canonicos primero."""
    grupos: dict[str, list] = {}
    for c in items:
        grupos.setdefault(c.marketplace, []).append(c)
    claves = [m for m in _MP_CANONICOS if m in grupos] + sorted(
        m for m in grupos if m not in _MP_CANONICOS
    )
    out = {m: _stats_precios(grupos[m]) for m in claves}
    out["global"] = _stats_precios(items)
    return out


@router.post("/api/productos/{producto_id}/competencia/buscar")
def buscar_competencia(producto_id: int, body: CompetenciaBuscarBody, db: SesionDep):
    """Busca candidatos de competencia via Claude web_search. NO persiste.

    Los sitios donde busca salen de la categoria del producto (columna
    Categoria.competencia_sitios); NULL/vacio => los 3 default. El campo
    `sitios_extra` del request se SUMA a esos sitios.
    """
    from app import competencia

    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")
    query = (body.query or "").strip() or competencia.construir_query(p)

    sitios_categoria = None
    if p.categoria:
        cat = db.query(Categoria).filter_by(slug=p.categoria).first()
        if cat:
            sitios_categoria = cat.competencia_sitios
    dominios = competencia.resolver_dominios(sitios_categoria, body.sitios_extra)
    return competencia.buscar_candidatos(query, dominios)


@router.get("/api/productos/{producto_id}/competencia")
def listar_competencia(producto_id: int, db: SesionDep):
    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")
    items = (
        db.query(CompetidorListing)
        .filter_by(producto_id=producto_id)
        .order_by(CompetidorListing.marketplace, CompetidorListing.precio_mxn)
        .all()
    )
    return {
        "items": [_competidor_to_dict(c) for c in items],
        "resumen": _resumen_competencia(items),
    }


@router.post("/api/productos/{producto_id}/competencia")
def guardar_competencia(producto_id: int, body: CompetenciaGuardarBody, db: SesionDep):
    """Persiste los listings confirmados por el usuario."""
    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")
    if not body.items:
        raise HTTPException(400, "Sin items para guardar")
    guardados = []
    for it in body.items:
        c = CompetidorListing(
            producto_id=producto_id,
            marketplace=it.marketplace,
            titulo=it.titulo,
            precio_mxn=it.precio_mxn,
            rating=it.rating,
            num_reviews=it.num_reviews,
            vendedor=it.vendedor,
            url=it.url,
            imagen_url=it.imagen_url,
            busqueda_query=(body.query or None),
            notas=(it.notas or None),
        )
        db.add(c)
        guardados.append(c)
    db.commit()
    for c in guardados:
        db.refresh(c)
    items = (
        db.query(CompetidorListing)
        .filter_by(producto_id=producto_id)
        .order_by(CompetidorListing.marketplace, CompetidorListing.precio_mxn)
        .all()
    )
    return {
        "items": [_competidor_to_dict(c) for c in items],
        "resumen": _resumen_competencia(items),
        "guardados": len(guardados),
    }


@router.delete("/api/productos/{producto_id}/competencia/{listing_id}")
def borrar_competidor(producto_id: int, listing_id: int, db: SesionDep):
    c = db.get(CompetidorListing, listing_id)
    if not c or c.producto_id != producto_id:
        raise HTTPException(404, "Listing no existe")
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
_FRACCION_RE = _re.compile(r"^\d{4}\.\d{2}\.\d{2}$")  # TIGIE NNNN.NN.NN


class CategoriaBody(BaseModel):
    slug: str
    orden: int = 100


class KeywordsBody(BaseModel):
    keywords: list[str]


class PatronDescarteBody(BaseModel):
    patron: str
    nota: str | None = None


class PropuestaItem(BaseModel):
    slug: str
    orden: int = 100
    keywords: list[str] = []
    fraccion: str | None = None
    tasa_pct: float | None = None
    arancel_nota: str | None = None
    arancel_fuente_url: str | None = None


class AplicarPropuestaBody(BaseModel):
    items: list[PropuestaItem]


class ArancelCategoriaBody(BaseModel):
    fraccion: str | None = None
    tasa_pct: float | None = None
    arancel_nota: str | None = None


class CompetenciaSitiosBody(BaseModel):
    # CSV de dominios o URLs; vacio => la categoria usa los 3 sitios default.
    competencia_sitios: str | None = None


def _cat_a_dict(c: Categoria, conteo_productos: int) -> dict:
    return {
        "id": c.id,
        "slug": c.slug,
        "orden": c.orden,
        "keywords": sorted(kw.keyword for kw in c.keywords),
        "productos": conteo_productos,
        "fraccion": c.fraccion,
        "tasa_pct": c.tasa_pct,
        "arancel_estado": c.arancel_estado,
        "arancel_nota": c.arancel_nota,
        "arancel_fuente_url": c.arancel_fuente_url,
        "competencia_sitios": c.competencia_sitios,
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
def reclasificar_sin_categoria(db: SesionDep, request: Request):
    """Aplica el clasificador a productos con categoria IS NULL.

    No toca productos ya clasificados. Devuelve {asignados, sin_match}.
    """
    slug = _slug_activo(request)
    invalidar_cache()
    asignados = 0
    sin_match = 0
    for p in db.query(Producto).filter(Producto.categoria.is_(None)).all():
        cat = clasificar_descripcion(p.descripcion, slug)
        if cat is None:
            sin_match += 1
        else:
            p.categoria = cat
            asignados += 1
    db.commit()
    return {"asignados": asignados, "sin_match": sin_match}


# ------------------------------------------------------------------
# Bootstrapping de catalogo asistido por IA (propuesta -> revision -> aplicar)
# ------------------------------------------------------------------

# Placeholder de descripcion que dejan algunos PDFs cuando el extractor no
# encuentra un nombre real (ej. "Product 223"). Se tratan como "sin descripcion".
_DESC_PLACEHOLDER_RE = _re.compile(r"^product\s+\d+$", _re.IGNORECASE)


def _necesita_descripcion(desc: str | None) -> bool:
    """True si la descripcion esta vacia o es un placeholder tipo 'Product 223'."""
    d = (desc or "").strip()
    return (not d) or bool(_DESC_PLACEHOLDER_RE.match(d))


def _sin_descripcion_filtro():
    """Clausula SQLAlchemy: descripcion vacia o placeholder 'Product N'."""
    return (
        Producto.descripcion.is_(None)
        | (func.trim(Producto.descripcion) == "")
        | Producto.descripcion.op("GLOB")("[Pp]roduct [0-9]*")
    )


def _descripciones_proyecto(session) -> list[str]:
    """Descripciones utiles del proyecto para la propuesta IA.

    Excluye '_descartar', vacias y placeholders 'Product N'.
    """
    rows = (
        session.query(Producto.descripcion)
        .filter(Producto.descripcion.isnot(None))
        .filter((Producto.categoria.is_(None)) | (Producto.categoria != "_descartar"))
        .distinct()
        .all()
    )
    return [d for (d,) in rows if d and not _necesita_descripcion(d)]


def _describir_sin_descripcion(
    session, slug: str, proveedor_id: int | None = None, forzar: bool = False
) -> dict:
    """Corre vision sobre productos con foto y persiste descripcion + tags.

    Por defecto solo toca los que NO tienen descripcion real (vacia o
    'Product N'). Con forzar=True redescribe TAMBIEN los que ya tienen una
    (para arreglar descripciones malas). Opcionalmente acotado a un proveedor.
    La categoria solo se rellena si esta vacia (no pisa ajustes manuales).
    Devuelve {descritos, clasificados, sin_foto, total, aviso, error}.
    """
    from app import catalogo_ia

    base = Path(_base_fotos(slug))
    q = session.query(Producto)
    if not forzar:
        q = q.filter(_sin_descripcion_filtro())
    if proveedor_id is not None:
        q = q.filter(Producto.proveedor_id == proveedor_id)
    prods = q.all()

    items, sin_foto = [], 0
    for p in prods:
        foto = (
            session.query(Foto)
            .filter_by(producto_id=p.id)
            .order_by(Foto.es_principal.desc())
            .first()
        )
        ruta = (base / foto.ruta_relativa) if foto else None
        if ruta is None or not ruta.exists():
            sin_foto += 1
            continue
        items.append({"producto_id": p.id, "sku": p.sku,
                      "medidas": p.medidas, "path": str(ruta)})

    if not items:
        return {"descritos": 0, "sin_foto": sin_foto, "total": len(prods),
                "aviso": None, "error": None}

    res = catalogo_ia.describir_fotos(items)
    if not res.get("ok"):
        return {"descritos": 0, "sin_foto": sin_foto, "total": len(prods),
                "aviso": None, "error": res.get("error")}

    descritos = 0
    descritos_ids = []
    for pid, d in res.get("resultados", {}).items():
        p = session.get(Producto, pid)
        if p is None:
            continue
        p.descripcion = d["descripcion"]
        if d.get("tags"):
            p.tags = ", ".join(d["tags"])
        descritos += 1
        descritos_ids.append(pid)
    session.commit()

    # Clasificar los productos recien descritos que sigan sin categoria, usando
    # el catalogo del proyecto (keywords). Si el catalogo esta vacio, quedan sin
    # categoria hasta que se proponga/defina uno.
    invalidar_cache()
    clasificados = 0
    for pid in descritos_ids:
        p = session.get(Producto, pid)
        if p is None or p.categoria is not None:
            continue
        cat = clasificar_descripcion(p.descripcion, slug)
        if cat is not None:
            p.categoria = cat
            clasificados += 1
    session.commit()

    return {"descritos": descritos, "clasificados": clasificados,
            "sin_foto": sin_foto, "total": len(prods),
            "aviso": res.get("aviso"), "error": None}


def _worker_describir_fotos(
    job_id: str, slug: str, proveedor_id: int | None = None, forzar: bool = False
) -> None:
    """Genera descripcion + tags por vision para productos con foto (job async).
    Por defecto solo los que no tienen descripcion real; con forzar=True tambien
    redescribe los que ya tienen una. Sesion propia ligada al slug.
    """
    try:
        SessionFactory = db_module.get_session_factory(slug)
        with SessionFactory() as session:
            r = _describir_sin_descripcion(session, slug, proveedor_id, forzar)
        if r["error"]:
            jobs.marcar_listo(job_id, 422, {"ok": False, "error": r["error"]})
            return
        jobs.marcar_listo(job_id, 200, {
            "ok": True, "descritos": r["descritos"],
            "clasificados": r.get("clasificados", 0), "sin_foto": r["sin_foto"],
            "total_sin_descripcion": r["total"], "aviso": r["aviso"],
        })
    except Exception as e:  # red de seguridad
        jobs.marcar_listo(job_id, 500, {"ok": False, "error": f"Error inesperado: {e}"})


@router.post("/api/catalogo/describir-fotos", status_code=202)
def describir_fotos_ia(request: Request, db: SesionDep, body: DescribirFotosBody | None = None):
    """Arranca la descripcion por vision de productos con foto (async).

    Por defecto solo los que NO tienen descripcion real (util para catalogos que
    solo traen SKU + foto + medidas). Con `forzar`=true redescribe TAMBIEN los que
    ya tienen una (para arreglar descripciones malas), y `proveedor_id` acota el
    alcance. Rellena `descripcion` + `tags` y categoria (solo si estaba vacia).
    202 + job_id.
    """
    slug = _slug_activo(request)
    forzar = bool(body and body.forzar)
    proveedor_id = body.proveedor_id if body else None
    q = db.query(func.count(func.distinct(Producto.id))).join(
        Foto, Foto.producto_id == Producto.id
    )
    if not forzar:
        q = q.filter(_sin_descripcion_filtro())
    if proveedor_id is not None:
        q = q.filter(Producto.proveedor_id == proveedor_id)
    n = q.scalar()
    if not n:
        raise HTTPException(
            400,
            "No hay productos con foto en el alcance seleccionado."
            if forzar else
            "No hay productos sin descripcion que tengan foto.",
        )
    job_id = jobs.crear_job("describir-fotos")
    hilo = threading.Thread(
        target=_worker_describir_fotos,
        args=(job_id, slug, proveedor_id, forzar),
        daemon=True,
    )
    hilo.start()
    return {"job_id": job_id, "estado": "procesando", "pendientes": int(n)}


@router.get("/api/catalogo/describir-fotos/status/{job_id}")
def estado_describir_fotos(job_id: str):
    job = jobs.obtener_job(job_id)
    if job is None:
        raise HTTPException(404, "Job no encontrado (puede que el servidor se haya reiniciado).")
    return job


@router.post("/api/productos/{producto_id}/redescribir")
def redescribir_producto(producto_id: int, request: Request, db: SesionDep):
    """Regenera la descripcion + tags de UN producto con IA a partir de su foto,
    y lo categoriza si no tiene categoria. Sincrono (una imagen). Sirve para
    arreglar descripciones malas producto por producto desde el panel de detalle.
    """
    from app import catalogo_ia

    slug = _slug_activo(request)
    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")

    foto = (
        db.query(Foto)
        .filter_by(producto_id=p.id)
        .order_by(Foto.es_principal.desc())
        .first()
    )
    ruta = (Path(_base_fotos(slug)) / foto.ruta_relativa) if foto else None
    if ruta is None or not ruta.exists():
        raise HTTPException(400, "El producto no tiene foto; no se puede describir con IA.")

    res = catalogo_ia.describir_fotos(
        [{"producto_id": p.id, "sku": p.sku, "medidas": p.medidas, "path": str(ruta)}]
    )
    if not res.get("ok"):
        raise HTTPException(502, res.get("error") or "La IA no pudo describir el producto.")
    d = res.get("resultados", {}).get(p.id)
    if not d:
        raise HTTPException(502, res.get("aviso") or "La IA no devolvio descripcion.")

    p.descripcion = d["descripcion"]
    if d.get("tags"):
        p.tags = ", ".join(d["tags"])
    db.commit()

    # Categoria: solo si esta vacia (no pisa ajustes manuales). Usa el catalogo
    # del proyecto (keywords); si esta vacio, queda sin categoria.
    invalidar_cache()
    clasificado = None
    if p.categoria is None:
        cat = clasificar_descripcion(p.descripcion, slug)
        if cat is not None:
            p.categoria = cat
            clasificado = cat
            db.commit()

    return {
        "ok": True,
        "producto_id": p.id,
        "descripcion": p.descripcion,
        "tags": p.tags,
        "categoria": p.categoria,
        "clasificado": clasificado,
        "aviso": res.get("aviso"),
    }


@router.get("/api/mantenimiento/indigest")
def analizar_indigest():
    """Previsualiza (sin borrar) los artefactos de trabajo y PDFs huerfanos que
    se pueden purgar de la carpeta de ingesta."""
    from app import mantenimiento
    return mantenimiento.analizar()


@router.post("/api/mantenimiento/indigest/limpiar")
def limpiar_indigest():
    """Purga extracciones de Adobe (_adobe_extract_*), intermedios y PDFs
    huerfanos de intentos fallidos. Conserva los PDFs referenciados por productos
    ('Cotizacion original')."""
    from app import mantenimiento
    return mantenimiento.limpiar()


def _worker_proponer_catalogo(job_id: str, slug: str) -> None:
    """Corre la propuesta de catalogo (categorias + aranceles IA) en un hilo.

    No persiste nada: deja la propuesta en el registro de jobs para que el front
    la muestre. Sesion propia ligada al slug capturado al encolar.
    """
    from app import catalogo_ia

    try:
        SessionFactory = db_module.get_session_factory(slug)
        with SessionFactory() as session:
            descripciones = _descripciones_proyecto(session)
            # Catalogo actual: la IA lo reutiliza (no duplica categorias al re-proponer).
            existentes = [
                {"slug": c.slug, "keywords": [kw.keyword for kw in c.keywords]}
                for c in session.query(Categoria).order_by(Categoria.orden.asc()).all()
            ]
        # Las llamadas a la IA no necesitan la BD; corren fuera de la sesion.
        propuesta = catalogo_ia.proponer_catalogo(descripciones, categorias_existentes=existentes)
        http_status = 200 if propuesta.get("ok") else 422
        jobs.marcar_listo(job_id, http_status, propuesta)
    except Exception as e:  # red de seguridad: nunca dejar el job colgado
        jobs.marcar_listo(job_id, 500, {"ok": False, "error": f"Error inesperado: {e}"})


@router.post("/api/catalogo/proponer-ia", status_code=202)
def proponer_catalogo_ia(request: Request, db: SesionDep):
    """Arranca en segundo plano la propuesta de catalogo con IA.

    Responde 202 + job_id de inmediato (la fase con web_search tarda). El front
    consulta GET /api/catalogo/proponer-ia/status/{job_id}.
    """
    slug = _slug_activo(request)
    total = db.query(func.count(Producto.id)).scalar() or 0
    # Contamos solo productos con descripcion textual: la propuesta se basa en
    # descripciones. Hay catalogos (SKU + foto + medidas, sin nombre) donde todas
    # vienen vacias -> no hay nada que analizar por texto.
    n = (
        db.query(func.count(Producto.id))
        .filter(Producto.descripcion.isnot(None))
        .filter(func.trim(Producto.descripcion) != "")
        .filter((Producto.categoria.is_(None)) | (Producto.categoria != "_descartar"))
        .scalar()
    )
    if not n:
        if total:
            raise HTTPException(
                400,
                f"Los {total} productos del proyecto no tienen descripcion textual "
                "(p. ej. catalogo por SKU + foto + medidas). La propuesta por IA se "
                "basa en descripciones; no hay nada que analizar por texto.",
            )
        raise HTTPException(400, "No hay productos para analizar. Ingesta productos primero.")
    job_id = jobs.crear_job("propuesta-catalogo")
    hilo = threading.Thread(
        target=_worker_proponer_catalogo, args=(job_id, slug), daemon=True
    )
    hilo.start()
    return {"job_id": job_id, "estado": "procesando"}


@router.get("/api/catalogo/proponer-ia/status/{job_id}")
def estado_proponer_catalogo(job_id: str):
    """Estado del job de propuesta (mismo contrato que el status de ingest)."""
    job = jobs.obtener_job(job_id)
    if job is None:
        raise HTTPException(404, "Job no encontrado (puede que el servidor se haya reiniciado).")
    return job


@router.post("/api/catalogo/aplicar-propuesta")
def aplicar_propuesta(body: AplicarPropuestaBody, db: SesionDep):
    """Persiste las categorias revisadas por el usuario (upsert por slug).

    Una categoria con fraccion valida (NNNN.NN.NN) + tasa queda 'confirmado' (a
    partir de aqui SI afecta la cotizacion); las demas 'pendiente'. Reemplaza las
    keywords. Devuelve conteos + reclasificar:True para que el front reclasifique.
    """
    from datetime import datetime

    if not body.items:
        raise HTTPException(400, "Sin items para aplicar.")

    creadas = actualizadas = confirmadas = pendientes = 0
    for it in body.items:
        slug = _validar_slug(it.slug)
        frac = (it.fraccion or "").strip() or None
        if frac and not _FRACCION_RE.match(frac):
            frac = None  # formato invalido -> se trata como sin fraccion
        tasa = it.tasa_pct
        if frac and tasa is not None:
            estado = "confirmado"
            confirmadas += 1
        else:
            estado = "pendiente"
            pendientes += 1

        cat = db.query(Categoria).filter_by(slug=slug).first()
        if cat is None:
            cat = Categoria(slug=slug, orden=it.orden)
            db.add(cat)
            db.flush()
            creadas += 1
        else:
            cat.orden = it.orden
            actualizadas += 1

        cat.fraccion = frac
        cat.tasa_pct = tasa
        cat.arancel_estado = estado
        cat.arancel_nota = (it.arancel_nota or "").strip() or None
        cat.arancel_fuente_url = (it.arancel_fuente_url or "").strip() or None
        cat.arancel_actualizado_en = datetime.utcnow()

        # Reemplazar keywords (normalizadas: lowercase, sin duplicados/vacios).
        limpias, vistas = [], set()
        for kw in it.keywords:
            k = (kw or "").strip().lower()
            if k and k not in vistas:
                vistas.add(k)
                limpias.append(k)
        db.query(CategoriaKeyword).filter_by(categoria_id=cat.id).delete()
        for k in limpias:
            db.add(CategoriaKeyword(categoria_id=cat.id, keyword=k))

    db.commit()
    invalidar_cache()
    return {
        "creadas": creadas,
        "actualizadas": actualizadas,
        "confirmadas": confirmadas,
        "pendientes": pendientes,
        "reclasificar": True,
    }


@router.patch("/api/catalogo/categorias/{cat_id}/arancel")
def editar_arancel_categoria(cat_id: int, body: ArancelCategoriaBody, db: SesionDep):
    """Fija/edita a mano la fraccion+tasa de una categoria (para las pendientes).

    Con fraccion valida + tasa queda 'confirmado'; si no, 'pendiente'.
    """
    from datetime import datetime

    c = db.get(Categoria, cat_id)
    if not c:
        raise HTTPException(404, "Categoria no existe")
    frac = (body.fraccion or "").strip() or None
    if frac and not _FRACCION_RE.match(frac):
        raise HTTPException(422, "Fraccion invalida: usa el formato NNNN.NN.NN")
    c.fraccion = frac
    c.tasa_pct = body.tasa_pct
    c.arancel_estado = "confirmado" if (frac and body.tasa_pct is not None) else "pendiente"
    c.arancel_nota = (body.arancel_nota or "").strip() or None
    c.arancel_actualizado_en = datetime.utcnow()
    db.commit()
    n = (
        db.query(func.count(Producto.id))
        .filter(Producto.categoria == c.slug)
        .scalar()
    )
    db.refresh(c)
    return _cat_a_dict(c, int(n or 0))


@router.patch("/api/catalogo/categorias/{cat_id}/competencia-sitios")
def editar_competencia_sitios(cat_id: int, body: CompetenciaSitiosBody, db: SesionDep):
    """Fija/edita los sitios donde buscar competencia para una categoria.

    Recibe dominios o URLs (separados por coma/espacio); se normalizan a hosts
    limpios, sin duplicados. Vacio => la categoria usa los 3 sitios default.
    """
    from datetime import datetime

    from app import competencia

    c = db.get(Categoria, cat_id)
    if not c:
        raise HTTPException(404, "Categoria no existe")
    dominios: list[str] = []
    for token in _re.split(r"[,\s;]+", body.competencia_sitios or ""):
        host = competencia._host_de_url(token)
        if host and host not in dominios:
            dominios.append(host)
    c.competencia_sitios = ",".join(dominios) or None
    c.competencia_actualizado_en = datetime.utcnow()
    db.commit()
    n = (
        db.query(func.count(Producto.id))
        .filter(Producto.categoria == c.slug)
        .scalar()
    )
    db.refresh(c)
    return _cat_a_dict(c, int(n or 0))


@router.get("/api/catalogo/exportar")
def exportar_catalogo(request: Request, db: SesionDep):
    """Descarga el catalogo del proyecto activo (categorias+aranceles+overrides+
    patrones) como JSON portable. Para mover config entre entornos sin re-IA."""
    import json as _json

    from app import catalogo_io

    # El ProyectoMiddleware ya garantiza un proyecto activo para /api/*; el
    # fallback solo cubre el bypass de tests. get_db ya resolvio la BD correcta.
    slug = request.session.get("proyecto") or db_module.PROYECTO_POR_DEFECTO
    data = catalogo_io.exportar_catalogo(db, proyecto=slug)
    # Serializamos aca (indentado, legible) y devolvemos ESE cuerpo tal cual con
    # Response: asi Starlette calcula el Content-Length correcto. Con JSONResponse
    # el cuerpo se re-serializaba compacto y el Content-Length manual (del texto
    # indentado) quedaba mas grande -> el navegador creia la descarga truncada.
    contenido = _json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=contenido,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="catalogo-{slug}.json"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/api/catalogo/importar")
async def importar_catalogo(request: Request, db: SesionDep, archivo: UploadFile = File(...)):
    """Importa (upsert, sin borrar) un catalogo JSON exportado en otro entorno.

    Trae categorias/aranceles/overrides/patrones a la BD del proyecto activo.
    Reclasifica al final porque las keywords/categorias pudieron cambiar.
    """
    import json as _json

    from app import catalogo_io

    crudo = await archivo.read()
    try:
        data = _json.loads(crudo.decode("utf-8"))
    except (UnicodeDecodeError, _json.JSONDecodeError) as e:
        raise HTTPException(422, f"El archivo no es JSON valido: {e}")

    try:
        res = catalogo_io.importar_catalogo(db, data)
    except catalogo_io.CatalogoInvalido as e:
        raise HTTPException(422, str(e))

    invalidar_cache()  # categorias/keywords cambiaron -> refrescar clasificador
    return res


@router.get("/categorias", response_class=HTMLResponse)
def pagina_categorias(request: Request, db: SesionDep):
    """Pagina CRUD del catalogo de categorias y patrones de descarte."""
    sin_categoria = (
        db.query(func.count(Producto.id))
        .filter(Producto.categoria.is_(None))
        .scalar()
    )
    # Productos sin descripcion textual que tienen foto (candidatos a describir
    # por vision). Ej. catalogos SKU + foto + medidas sin nombre.
    sin_descripcion = (
        db.query(func.count(func.distinct(Producto.id)))
        .join(Foto, Foto.producto_id == Producto.id)
        .filter(_sin_descripcion_filtro())
        .scalar()
    )
    return TEMPLATES.TemplateResponse(
        request,
        "categorias.html",
        {
            "productos_sin_categoria": int(sin_categoria or 0),
            "productos_sin_descripcion": int(sin_descripcion or 0),
        },
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
def listar_usuarios(db: SistemaDep):
    items = (
        db.query(UsuarioAutorizado)
        .order_by(UsuarioAutorizado.activo.desc(), UsuarioAutorizado.email)
        .all()
    )
    return {"items": [_usuario_to_dict(u) for u in items]}


@router.post("/api/usuarios")
def crear_usuario(body: UsuarioCreateBody, db: SistemaDep):
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
    usuario_id: int, body: UsuarioPatchBody, request: Request, db: SistemaDep
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
def borrar_usuario(usuario_id: int, request: Request, db: SistemaDep):
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


@router.get("/api/ayuda")
def api_ayuda():
    """Contenido del manual de usuario (Markdown) para la pagina /ayuda y el modal '?'."""
    return help_content.cargar_ayuda()


@router.get("/ayuda", response_class=HTMLResponse)
def pagina_ayuda(request: Request):
    return TEMPLATES.TemplateResponse(request, "ayuda.html", {})
