from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import db as db_module
from app.modelos import Producto

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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
    p = db.get(Producto, producto_id)
    if not p:
        raise HTTPException(404, "Producto no existe")
    p.marcado_cotizar = body.marcado
    db.commit()
    return {"ok": True, "marcado": p.marcado_cotizar}


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
    db.commit()
    return {"ok": True}


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: SesionDep):
    productos = db.query(Producto).limit(500).all()
    return TEMPLATES.TemplateResponse(
        "productos.html",
        {"request": request, "productos": productos},
    )


@router.get("/exportar")
def exportar(db: SesionDep):
    """Genera xlsx intermedio + ejecuta llenar_formato_hd.py."""
    import subprocess
    import sys as _sys
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

    # Borrar salida previa para evitar prompt interactivo de sobreescritura
    salida_esperada = proyecto / f"formato-hd-{xlsx_int.stem.lower()}.xlsx"
    if salida_esperada.exists():
        try:
            salida_esperada.unlink()
        except PermissionError:
            raise HTTPException(
                500,
                f"No puedo borrar el archivo anterior (esta abierto en Excel?): {salida_esperada.name}",
            )

    result = subprocess.run(
        [_sys.executable, str(script), str(xlsx_int), str(formato)],
        capture_output=True, text=True, cwd=str(proyecto),
    )
    if result.returncode != 0:
        raise HTTPException(500, f"Fallo llenar_formato_hd: {result.stderr[:500]}")

    if not salida_esperada.exists():
        raise HTTPException(500, f"No encontre el archivo de salida: {salida_esperada.name}")

    return FileResponse(str(salida_esperada), filename=salida_esperada.name)
