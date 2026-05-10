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
    assert n2 == 0
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
    p.fob_usd = 99.0
    db_session.commit()

    ingestar_xlsx_intermedio(db_session, str(fixture), "V", str(fotos_dir))
    db_session.commit()

    p2 = db_session.query(Producto).filter_by(sku="TEST-001").first()
    assert p2.fob_usd == 10.5
