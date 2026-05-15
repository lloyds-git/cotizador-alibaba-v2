"""
CLI para ingestar todos los _intermedio_*.xlsx que esten en la raiz del proyecto
a la BD. Tambien permite ingestar uno solo por nombre.

Uso:
    python -m app.cli init                  # crear BD vacia
    python -m app.cli ingestar              # todos los _intermedio_*.xlsx
    python -m app.cli ingestar archivo.xlsx # uno especifico
    python -m app.cli ingestar --force      # saltea el gate de calidad
    python -m app.cli pdf archivo.pdf       # procesa PDF e ingesta
    python -m app.cli stats                 # contar productos/proveedores
    python -m app.cli validar [prov_id]     # reporte de calidad (default: ultimo)
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.db import get_session_factory, init_db, DB_PATH
from app.ingest import ingestar_xlsx_intermedio, resolver_nombre_proveedor
from app.modelos import Proveedor, Producto


PROYECTO_ROOT = Path(__file__).parent.parent
FOTOS_DIR = PROYECTO_ROOT / "data" / "fotos"


def _pdf_ingest_dir() -> Path:
    """Carpeta donde se dejan los PDFs a procesar. Configurable via PDF_INGEST_DIR."""
    load_dotenv()
    nombre = os.environ.get("PDF_INGEST_DIR") or "indigest-pdf"
    p = Path(nombre)
    if not p.is_absolute():
        p = PROYECTO_ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolver_archivo(nombre: str, exts: tuple[str, ...] | None = None) -> Path | None:
    """
    Busca un archivo por nombre. Orden de busqueda:
      1. Tal cual (absoluto o relativo al CWD)
      2. Dentro de PDF_INGEST_DIR
    Si exts esta dado, filtra por extension al buscar dentro de PDF_INGEST_DIR.
    """
    p = Path(nombre)
    if p.is_absolute() and p.exists():
        return p
    candidato = PROYECTO_ROOT / nombre
    if candidato.exists():
        return candidato
    candidato = _pdf_ingest_dir() / nombre
    if candidato.exists():
        return candidato
    return None


def cmd_init():
    init_db()
    print(f"BD inicializada en: {DB_PATH}")


def cmd_ingestar(patron: str | None = None, *, gate: bool = True):
    """Ingesta _intermedio_*.xlsx a la BD.

    Si gate=True (default), tras ingestar valida calidad y hace rollback
    automatico si el veredicto es REINGESTAR. Las fotos fisicas creadas en
    la corrida tambien se borran del disco.
    """
    if not DB_PATH.exists():
        init_db()

    SessionFactory = get_session_factory()
    s = SessionFactory()

    ingest_dir = _pdf_ingest_dir()
    if patron:
        resuelto = _resolver_archivo(patron)
        archivos = [resuelto] if resuelto else [PROYECTO_ROOT / patron]
    else:
        archivos = sorted(
            list(PROYECTO_ROOT.glob("_intermedio_*.xlsx"))
            + list(ingest_dir.glob("_intermedio_*.xlsx"))
        )

    if not archivos:
        print(f"No hay _intermedio_*.xlsx para procesar (busque en {PROYECTO_ROOT} y {ingest_dir}).")
        return

    total_nuevos = 0
    bloqueados = 0
    for xlsx in archivos:
        if not xlsx.exists():
            print(f"  No existe: {xlsx}")
            continue
        # El nombre real lo resuelve ingest (lee .meta.json si existe).
        # Lo calculamos aca tambien para encontrar el proveedor post-flush en el gate.
        nombre = resolver_nombre_proveedor(str(xlsx))
        print(f"  Proveedor: {nombre}")
        # Snapshot de fotos fisicas pre-ingest para poder limpiar si hay rollback
        fotos_antes = set(FOTOS_DIR.glob("*")) if FOTOS_DIR.exists() else set()
        try:
            n = ingestar_xlsx_intermedio(
                session=s,
                xlsx_path=str(xlsx),
                nombre_proveedor=None,  # lo resuelve ingest desde .meta.json
                fotos_destino=str(FOTOS_DIR),
            )

            if gate:
                s.flush()
                prov = s.query(Proveedor).filter_by(nombre=nombre).first()
                prods = (
                    s.query(Producto).filter_by(proveedor_id=prov.id).all()
                    if prov else []
                )
                info = _calcular_veredicto(prods)
                if info["veredicto"] == "REINGESTAR":
                    s.rollback()
                    fotos_nuevas = set(FOTOS_DIR.glob("*")) - fotos_antes
                    for f in fotos_nuevas:
                        try:
                            f.unlink()
                        except OSError:
                            pass
                    print(f"  {xlsx.name}: BLOQUEADO por gate de calidad.")
                    print(f"    Motivo: {info['motivo']}")
                    print(f"    Rollback de BD + {len(fotos_nuevas)} fotos fisicas borradas.")
                    print(f"    Sugerencia: re-procesar el PDF con --claude (o usar --force).")
                    bloqueados += 1
                    continue
                if info["veredicto"] == "REVISAR":
                    print(f"  {xlsx.name}: ADVERTENCIA gate de calidad.")
                    print(f"    Motivo: {info['motivo']}")
                    print(f"    Se commitea igual; usa 'app.cli validar' para detalles.")

            s.commit()
            print(f"  {xlsx.name}: +{n} productos nuevos")
            total_nuevos += n
        except Exception as e:
            s.rollback()
            print(f"  ERROR {xlsx.name}: {e}")

    print(f"\nTotal productos nuevos: {total_nuevos}", end="")
    if bloqueados:
        print(f"  (bloqueados por gate: {bloqueados})")
    else:
        print()
    s.close()


def cmd_pdf(pdf_path: str):
    """Procesa un PDF nuevo y lo ingesta a la BD.

    El PDF puede pasarse como ruta absoluta, relativa al CWD, o solo el nombre
    (se buscara en PDF_INGEST_DIR, por defecto ./indigest-pdf/).
    """
    import subprocess

    script = PROYECTO_ROOT / "pdf_a_formato_hd.py"
    if not script.exists():
        print(f"No existe el script: {script}")
        return

    pdf_resuelto = _resolver_archivo(pdf_path)
    if not pdf_resuelto:
        print(f"No encuentro {pdf_path} (busque en CWD, {PROYECTO_ROOT} y {_pdf_ingest_dir()}).")
        return

    result = subprocess.run(
        [sys.executable, str(script), str(pdf_resuelto)],
        cwd=str(PROYECTO_ROOT),
    )
    if result.returncode != 0:
        print("Fallo pdf_a_formato_hd")
        return

    # El _intermedio_*.xlsx queda junto al PDF (en indigest-pdf/ o en PROYECTO_ROOT)
    carpeta_pdf = pdf_resuelto.parent
    intermedios = sorted(
        carpeta_pdf.glob("_intermedio_*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not intermedios:
        print(f"No se genero _intermedio_*.xlsx en {carpeta_pdf}")
        return

    cmd_ingestar(str(intermedios[0]))


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


# Palabras/patrones que delatan filas basura (headers/footers del PDF)
_SOSPECHOSA_KEYWORDS = (
    "quotation", "tel:", "adress:", "address:", "total cost",
    "no. model", "bio-tec", "@sina.com", "@alibaba",
)


def _es_sospechosa(p) -> str | None:
    """Devuelve motivo si el producto parece basura; None si parece OK."""
    sku = (p.sku or "").strip()
    desc = (p.descripcion or "").strip()
    desc_low = desc.lower()
    if not sku or sku.startswith("AUTO-"):
        return "SKU autogenerado (parser no detecto codigo real)"
    if "alibaba.com/product-detail" in desc_low:
        return "descripcion es URL de Alibaba"
    for k in _SOSPECHOSA_KEYWORDS:
        if k in desc_low:
            return f"descripcion contiene '{k}' (header/footer del PDF)"
    if len(desc) < 10 and not (p.material and len(p.material.strip()) >= 3):
        return f"descripcion muy corta ({len(desc)} chars) y sin material"
    if not p.fob_usd or p.fob_usd <= 0:
        return "FOB ausente o <= 0"
    return None


def _calcular_veredicto(prods) -> dict:
    """Analiza una lista de productos y devuelve metricas + veredicto.

    Veredicto:
      - "OK"          si SKUs reales >= 80% y sospechosas <= 20%
      - "REINGESTAR"  si SKUs reales < 50%
      - "REVISAR"     zona gris en el medio
    """
    n = len(prods)
    if n == 0:
        return {"n": 0, "veredicto": "VACIO", "cobertura": {}, "sospechosas": []}

    def vacio(v):
        return v is None or (isinstance(v, str) and not v.strip())

    campos = [
        ("SKU real (no AUTO-)", lambda p: p.sku and not p.sku.startswith("AUTO-")),
        ("FOB > 0", lambda p: p.fob_usd and p.fob_usd > 0),
        ("material", lambda p: not vacio(p.material)),
        ("medidas", lambda p: not vacio(p.medidas)),
        ("peso_kg", lambda p: p.peso_kg is not None),
        ("color", lambda p: not vacio(p.color)),
        ("moq", lambda p: not vacio(p.moq)),
        ("packing", lambda p: not vacio(p.packing)),
        ("cbm", lambda p: p.cbm is not None),
        ("lead_time", lambda p: not vacio(p.lead_time)),
        ("foto >= 1", lambda p: len(p.fotos) >= 1),
    ]
    cobertura = {nombre: sum(1 for p in prods if fn(p)) for nombre, fn in campos}
    sospechosas = [(p, _es_sospechosa(p)) for p in prods]
    sospechosas = [(p, m) for p, m in sospechosas if m]

    pct_sku = cobertura["SKU real (no AUTO-)"] / n
    pct_susp = len(sospechosas) / n

    if pct_sku >= 0.80 and pct_susp <= 0.20:
        veredicto = "OK"
        motivo = "SKUs reales >= 80%, sospechosas <= 20%"
    elif pct_sku < 0.50:
        veredicto = "REINGESTAR"
        motivo = f"Solo {pct_sku:.0%} de SKUs reales (umbral 50%)"
    else:
        veredicto = "REVISAR"
        motivo = f"SKUs reales {pct_sku:.0%}, sospechosas {pct_susp:.0%}"

    return {
        "n": n,
        "veredicto": veredicto,
        "motivo": motivo,
        "cobertura": cobertura,
        "sospechosas": sospechosas,
    }


def _imprimir_veredicto(info: dict, prefijo: str = "  "):
    """Imprime el resultado de _calcular_veredicto en formato legible."""
    n = info["n"]
    if n == 0:
        print(f"{prefijo}(vacio)")
        return
    print(f"{prefijo}--- Cobertura de campos ---")
    for nombre, ok in info["cobertura"].items():
        pct = 100 * ok / n
        barra = "#" * int(pct / 5)
        print(f"{prefijo}  {nombre:.<24} {ok:>3}/{n:<3} ({pct:>3.0f}%) {barra}")
    sospechosas = info["sospechosas"]
    print()
    print(f"{prefijo}--- Filas sospechosas: {len(sospechosas)}/{n} ---")
    for p, motivo in sospechosas[:10]:
        print(f"{prefijo}  id={p.id} sku={p.sku!r}")
        print(f"{prefijo}     motivo: {motivo}")
        print(f"{prefijo}     desc: {(p.descripcion or '')[:70]!r}")
    if len(sospechosas) > 10:
        print(f"{prefijo}  ... y {len(sospechosas)-10} mas")
    print()
    print(f"{prefijo}VEREDICTO: {info['veredicto']}  ({info['motivo']})")


def cmd_validar(proveedor_id: str | None = None):
    """Reporta calidad de la ingesta para un proveedor (default: el ultimo)."""
    SessionFactory = get_session_factory()
    s = SessionFactory()
    try:
        if proveedor_id:
            prov = s.query(Proveedor).get(int(proveedor_id))
        else:
            prov = s.query(Proveedor).order_by(Proveedor.id.desc()).first()
        if not prov:
            print("No hay proveedor para validar.")
            return
        prods = s.query(Producto).filter_by(proveedor_id=prov.id).all()
        print(f"Proveedor: {prov.nombre} (id={prov.id})")
        print(f"Productos ingestados: {len(prods)}")
        print()
        info = _calcular_veredicto(prods)
        _imprimir_veredicto(info, prefijo="")
    finally:
        s.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "init":
        cmd_init()
    elif cmd == "ingestar":
        args = [a for a in sys.argv[2:] if not a.startswith("--")]
        flags = [a for a in sys.argv[2:] if a.startswith("--")]
        patron = args[0] if args else None
        cmd_ingestar(patron, gate="--force" not in flags)
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "pdf":
        if len(sys.argv) < 3:
            print("Uso: python -m app.cli pdf <archivo.pdf>")
            return
        cmd_pdf(sys.argv[2])
    elif cmd == "validar":
        prov_id = sys.argv[2] if len(sys.argv) >= 3 else None
        cmd_validar(prov_id)
    else:
        print(f"Comando desconocido: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
