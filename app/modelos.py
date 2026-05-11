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
