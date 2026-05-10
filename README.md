# Base de Datos de Productos Mascotas

App local para centralizar productos extraidos de PDFs de cotizacion en una
BD SQLite, revisarlos con UI web, marcar candidatos a cotizar y regenerar el
formato HD final con los seleccionados.

## Instalacion

```bash
pip install -e ".[dev]"
python -m app.cli init
```

## Flujo tipico

1. Procesar PDFs nuevos:

   ```bash
   python -m app.cli pdf "cotizacion.pdf"
   ```

   Ejecuta el pipeline (Adobe + Haiku si hace falta) y mete los productos
   en la BD.

2. O ingestar todos los intermedios existentes:

   ```bash
   python -m app.cli ingestar
   ```

3. Arrancar UI:

   ```bash
   python -m app.main
   ```

   Abrir http://127.0.0.1:8080.

4. En la UI:
   - Ajustar tipo de cambio, factor importacion, margen objetivo.
   - Buscar productos por SKU o descripcion.
   - Marcar candidatos a cotizar.
   - Click "Exportar marcados" para descargar el formato HD final.

## Estructura

- `data/productos.db` -- SQLite con productos, proveedores, fotos.
- `data/fotos/` -- imagenes copiadas desde los intermedios.
- `_intermedio_*.xlsx` -- output del pipeline PDF, son ingeridos.
- `formato-hd-*.xlsx` -- formato HD final para enviar a Home Depot.

## Tests

```bash
pytest -v
```

## Arquitectura

| Modulo | Responsabilidad |
|--------|-----------------|
| `app/modelos.py` | ORM: `Proveedor`, `Producto`, `Foto` |
| `app/db.py` | Engine SQLite y session factory |
| `app/pricing.py` | Calculos de landed/retail/margen |
| `app/ingest.py` | Importar `_intermedio_*.xlsx` con idempotencia |
| `app/exportar.py` | Construir xlsx con marcados |
| `app/cli.py` | Comandos `init`/`ingestar`/`pdf`/`stats` |
| `app/main.py` | App FastAPI |
| `app/routes.py` | Endpoints CRUD + UI + exportar |
| `app/templates/` | HTML con HTMX + Alpine + Tailwind |

Los scripts existentes (`pdf_a_formato_hd.py`, `extraer_con_claude.py`,
`llenar_formato_hd.py`) NO se reescriben; se invocan desde la app.
