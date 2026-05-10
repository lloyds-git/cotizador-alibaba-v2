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
