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
    nw_caja_kg = Column(Float)  # N.W. del carton master; permite derivar pzas_caja = floor(nw/peso_kg)
    gw_caja_kg = Column(Float)  # G.W. del carton master; reservado para flete/peso volumetrico
    color = Column(String(200))
    moq = Column(String(50))
    packing = Column(String(200))
    carton_dims = Column(String(200))
    cbm = Column(Float)
    pzas_20ft = Column(Integer)
    pzas_40hq = Column(Integer)
    pzas_caja = Column(Integer)  # piezas por carton master (QTY/CARTON del PDF)
    lead_time = Column(String(100))

    categoria = Column(String(50))
    subcategoria = Column(String(100))

    # 'Primary' | 'Special Buy' (export Pet PD)
    item_type = Column(String(20), default="Primary")

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


class Categoria(Base):
    """Categoria de producto. Sembrada desde config/categorias.yml.

    El YAML es la fuente de verdad; esta tabla es una proyeccion sembrada
    via 'python -m app.cli seed-categorias'. El clasificador lee de aqui
    con cache (lru_cache); si la tabla esta vacia hace fallback al YAML.

    Orden: numero menor = mayor prioridad. Importante para resolver
    overlaps entre categorias (ej. 'hummingbird feeder' debe caer en
    'pajaros' antes que en 'alimentadores').
    """
    __tablename__ = "categorias"
    id = Column(Integer, primary_key=True)
    slug = Column(String(50), nullable=False, unique=True)
    orden = Column(Integer, nullable=False, default=100)

    keywords = relationship(
        "CategoriaKeyword",
        back_populates="categoria",
        cascade="all, delete-orphan",
    )


class CategoriaKeyword(Base):
    """Substring case-insensitive que dispara una categoria. NO es regex."""
    __tablename__ = "categoria_keywords"
    __table_args__ = (
        UniqueConstraint("categoria_id", "keyword", name="uq_categoria_keyword"),
    )
    id = Column(Integer, primary_key=True)
    categoria_id = Column(Integer, ForeignKey("categorias.id"), nullable=False)
    keyword = Column(String(100), nullable=False)

    categoria = relationship("Categoria", back_populates="keywords")


class PatronDescarte(Base):
    """Regex que marca una descripcion como '_descartar' (no es producto real).

    Captura notas de pricing, fragmentos de empaque y etiquetas sueltas
    que Claude tomo como producto al extraer del PI. Distinto de
    CategoriaKeyword (substring): aqui SI es regex case-insensitive.
    """
    __tablename__ = "patrones_descarte"
    id = Column(Integer, primary_key=True)
    patron = Column(String(200), nullable=False, unique=True)
    nota = Column(Text)


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


class UsuarioAutorizado(Base):
    """Lista blanca de correos con acceso a la app via Google OAuth2.

    El callback de OAuth (app/auth.py) rechaza el login si el correo no
    esta aqui o si activo=False. Sin roles: cualquier usuario activo puede
    gestionar la lista. Guardias en los endpoints impiden que alguien se
    desactive/borre a si mismo o al ultimo usuario activo.
    """
    __tablename__ = "usuarios_autorizados"
    id = Column(Integer, primary_key=True)
    email = Column(String(200), nullable=False, unique=True, index=True)
    nombre = Column(String(200))
    activo = Column(Boolean, default=True, nullable=False)
    creado_en = Column(DateTime, default=datetime.utcnow)
    ultimo_login = Column(DateTime)


class Arancel(Base):
    """Fraccion arancelaria estandar por (categoria, subcategoria).

    Reemplaza el dict hardcoded de app/cotizador/tariffs.py. Se siembra
    desde config/aranceles.yml. Editable desde la UI /aranceles.

    Las categoria/subcategoria aca son las del lookup_tariff (ej.
    'Mascotas'/'Jaulas'), no las categorias de Producto. El mapeo entre
    `Producto.categoria` (slug) -> ('Mascotas', 'Subcat') vive en
    app/cotizador/adapter.py CATEGORIA_A_TARIFA.

    Orden de resolucion: ArancelOverride > default-metal > Arancel > default-25.
    """
    __tablename__ = "aranceles"
    __table_args__ = (
        UniqueConstraint("categoria", "subcategoria", name="uq_arancel_cat_subcat"),
    )
    id = Column(Integer, primary_key=True)
    categoria = Column(String(50), nullable=False)
    subcategoria = Column(String(50), nullable=False)
    fraccion = Column(String(20), nullable=False)
    tasa_pct = Column(Float, nullable=False)
    nota = Column(Text)
    creado_en = Column(DateTime, default=datetime.utcnow)
    actualizado_en = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
