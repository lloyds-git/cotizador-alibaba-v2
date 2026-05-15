from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, Integer, case, cast
from sqlalchemy.orm import Session

from app import db as db_module
from app.modelos import Producto, Proveedor, ArancelOverride, CostoAdicional, CotizacionSnapshot

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
    lead_time: str | None = None


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
    """Devuelve categorias con total y cuantos marcados, ordenadas por total desc."""
    # CAST a Integer porque marcado_cotizar es Boolean y SQLAlchemy convierte
    # SUM(bool) -> bool (devuelve True en vez del conteo real).
    filas = (
        db.query(
            Producto.categoria,
            func.count(Producto.id).label("total"),
            func.sum(
                cast(
                    case((Producto.marcado_cotizar.is_(True), 1), else_=0),
                    Integer,
                )
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
        nombre_safe = ruta.name.replace('"', '\\"')
        # ?descargar=1 fuerza attachment; default es inline para que se
        # renderice en el <embed> del visor.
        disposition = "attachment" if descargar else "inline"
        return FileResponse(
            str(ruta),
            media_type="application/pdf",
            headers={"Content-Disposition": f'{disposition}; filename="{nombre_safe}"'},
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
        "lead_time": p.lead_time,
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
        p.categoria = body.categoria.strip() or None
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
    if body.lead_time is not None:
        p.lead_time = body.lead_time.strip() or None
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

    formato = proyecto / "Formato HD-Mascotas.xlsb"
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

    # El intermedio de exportar.py tiene 17 columnas (Foto=A, SKU=B, Descripcion=C,
    # ..., FOB USD=O, Retail c/IVA=P, Margen Lloyds=Q).
    # El default de llenar_formato_hd.py asume el layout viejo (22 cols), por
    # eso forzamos el mapeo.
    result = subprocess.run(
        [
            _sys.executable, str(script), str(xlsx_int), str(formato),
            # Mapeo correcto al HD destino:
            #   col C (Descripcion) -> fila 8  (DESCRIPTION)
            #   col O (FOB USD)     -> fila 11 (DOMESTIC COST)
            #   col P (Retail MXN)  -> fila 16 (SUGGESTED RETAIL)
            #   col Q (Margen)      -> fila 17 (THD MARGIN)
            "--mapeo", "C=8,O=11,P=16,Q=17", "--yes",
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


def _snapshot_to_dict(s: CotizacionSnapshot) -> dict:
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
    return {"items": [_snapshot_to_dict(s) for s in snaps]}


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
    return _snapshot_to_dict(snap)


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
        d = _snapshot_to_dict(s)
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
