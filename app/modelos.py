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
        # AUTOINCREMENT: SQLite no reusa ids despues de DELETE.
        # Protege referencias externas (exports xlsx, cotizaciones guardadas,
        # snapshots manuales) contra colisiones tras borrar+re-ingestar.
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), nullable=False)
    sku = Column(String(50))
    descripcion = Column(Text, nullable=False)

    fob_usd = Column(Float)
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

    categoria = Column(String(50))
    subcategoria = Column(String(100))

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
    ruta_relativa = Column(String(500), nullable=False)
    es_principal = Column(Boolean, default=False)

    producto = relationship("Producto", back_populates="fotos")


class CostoAdicional(Base):
    """Costos adicionales al FOB china por producto.

    Se usa para EXW->FOB, caja color, certificacion, retrabajo, etc.
    Se SUMAN al fob_usd del producto antes de pasar al motor 14 pasos.

    Permanente: queda registro de cuando/quien/cuanto se agrego para
    auditoria. Multiple costos por producto (caja+EXW+etc).
    """
    __tablename__ = "costos_adicionales"
    id = Column(Integer, primary_key=True)
    producto_id = Column(Integer, ForeignKey("productos.id"), nullable=False)
    concepto = Column(String(100), nullable=False)  # 'caja color', 'EXW->FOB', etc
    monto_usd = Column(Float, nullable=False)
    notas = Column(Text)
    creado_en = Column(DateTime, default=datetime.utcnow)

    producto = relationship("Producto", backref="costos_adicionales")


class CotizacionSnapshot(Base):
    """Snapshot historico de una cotizacion guardada.

    Permite trazabilidad: que retail/margen/settings usamos para cada
    producto en cada exportacion o ajuste manual. Cada export crea un
    snapshot automaticamente; tambien se puede guardar manual desde el
    panel ('Guardar cotizacion').

    Inmutable por convencion: las correcciones se hacen creando un nuevo
    snapshot, no editando el viejo.
    """
    __tablename__ = "cotizacion_snapshots"
    id = Column(Integer, primary_key=True)
    producto_id = Column(Integer, ForeignKey("productos.id"), nullable=False)
    creado_en = Column(DateTime, default=datetime.utcnow, nullable=False)
    origen = Column(String(50))  # 'manual', 'export', 'export-categoria'

    # FOB efectivo usado (fob_usd + suma costos adicionales al momento del snapshot)
    fob_usd_efectivo = Column(Float)
    costos_adicionales_usd = Column(Float, default=0.0)

    # Settings de cotizacion
    tc = Column(Float)
    flete_maritimo_usd = Column(Float)
    flete_local_mxn = Column(Float)
    margen_nuestro_pct = Column(Float)
    margen_cliente_pct = Column(Float)
    descuentos_pct = Column(Float)
    descuentos_na_pct = Column(Float)
    gasto_fijo_pct = Column(Float)
    gastos_aduanales_pct = Column(Float)

    # Resultado clave
    fraccion_arancelaria = Column(String(20))
    tasa_arancelaria_pct = Column(Float)
    landed_unit_mxn = Column(Float)        # paso 9
    venta_lloyds_mxn = Column(Float)        # paso 11 (motor) o derivada de retail
    retail_final_mxn = Column(Float)        # paso 13 o retail editado
    margen_real_pct = Column(Float)         # utilidad/venta computado

    # Contexto opcional
    archivo_exportado = Column(String(300)) # nombre del HD si se origino por export
    notas = Column(Text)

    producto = relationship("Producto", backref="snapshots")


class ArancelOverride(Base):
    """Overrides de fraccion arancelaria y tasa por categoria y material.

    Reglas de match (mas especifico gana):
      1. categoria + material_pattern (ambos no nulos): match exacto cat +
         material que contenga el patron (case-insensitive).
      2. categoria + material_pattern=NULL: aplica a toda la categoria.
      3. categoria=NULL + material_pattern: aplica a cualquier categoria
         con ese material (ej. todo lo que sea de acero).
      4. ambos NULL: fallback global (no debe haber mas de uno).

    Se consulta en lookup_tariff_db ANTES de la tabla estatica de tariffs.py.
    """
    __tablename__ = "aranceles_override"
    id = Column(Integer, primary_key=True)
    categoria = Column(String(50))           # nullable: aplica a todas
    material_pattern = Column(String(100))   # nullable: aplica a cualquier material
    fraccion = Column(String(20), nullable=False)
    tasa_pct = Column(Float, nullable=False)
    nota = Column(Text)
    creado_en = Column(DateTime, default=datetime.utcnow)
    actualizado_en = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
