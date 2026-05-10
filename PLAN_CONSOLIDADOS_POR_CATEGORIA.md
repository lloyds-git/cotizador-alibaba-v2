# Plan: Consolidados por categoría desde índice Acowork

## Contexto

El volumen de adjuntos en correos es demasiado grande para parsear todo de un jalón. Acowork ya generó un **índice Excel** con todos los adjuntos categorizados (rejas, viaje, mascotas, etc.) y con la liga directa a cada archivo. La estrategia es: Salo marca en el índice los archivos que le interesan, una **categoría a la vez**, y Claude genera un consolidado en el formato estándar de 20 columnas, parseando, enriqueciendo y cotizando solo esos archivos.

Tres preocupaciones reales que el plan debe resolver:
1. PDFs: la conversión externa con iLovePDF/Adobe ha sido más confiable para mantener foto-fila alineada que el parseo directo.
2. 39 archivos sin clasificar — necesitan triage antes de entrar al loop de categorías.
3. Archivos multi-categoría: parsear todo y luego filtrar rows que pertenecen a la categoría activa, no extraer el archivo entero.

---

## Insumo: el índice Acowork

Ubicación esperada (cuando lo copies a la máquina principal): `data/indices/acowork-YYYYMMDD.xlsx`.

Convención mínima que debe tener cada fila (si Acowork no las trae con estos nombres exactos, renómbralas a esto antes de empezar):

| Columna | Contenido |
|---|---|
| `correo_id` | ID del correo origen (o thread id) |
| `fecha` | Fecha del correo (YYYY-MM-DD) |
| `proveedor` | Nombre del proveedor (puede venir vacío, lo completa el parser) |
| `archivo` | Nombre del archivo adjunto |
| `liga` | URL / ruta al archivo (Drive, S3 o ruta local) |
| `extension` | xlsx / xls / pdf / png / jpg |
| `categoria` | rejas, viaje, mascotas, …, o `sin_clasificar` |
| `marcar` | Vacío por default; Salo pone `X` en las filas a procesar |
| `notas` | Salo puede dejar comentario por fila (opcional) |

Para archivos multi-categoría, Acowork debe duplicar la fila (una por categoría a la que pertenece) **o** Salo agrega una columna `subset_skus` con los SKUs / descripciones que pertenecen a la categoría activa (ver sección "Archivos multi-categoría" abajo).

---

## Workflow por categoría (loop)

Para cada categoría que Salo quiera procesar:

1. **Salo en su Excel:** filtra `categoria = <X>`, pone `X` en `marcar` para los archivos que sí quiere, exporta a `data/indices/seleccion-<categoria>-YYYYMMDD.xlsx` (solo las filas marcadas).

2. **Salo a Claude:** "procesa la selección de la categoría `<X>`, archivo `data/indices/seleccion-<categoria>-YYYYMMDD.xlsx`".

3. **Claude:**
   - Lee el subset.
   - Verifica que cada `liga` resuelva a un archivo local. Si vienen URLs (Drive/S3), descarga a `data/inbox/<categoria>-YYYYMMDD/`.
   - Para cada archivo, decide tipo de parseo (ver "Manejo de PDFs").
   - Corre `scripts/parse_folder.py` apuntando a esa carpeta — esto popula `parsed_quotes` en `alibaba.db` y deja un consolidado crudo en `data/out/`.
   - Aplica el filtro intra-archivo si aplica (multi-categoría).
   - Corre `scripts/enrich_xlsx.py` (deriva CBM/caja, pzas/contenedor, fracción, tasa).
   - Corre `scripts/cotizar_xlsx.py` con los parámetros default (ver "Cotización").
   - Genera el consolidado final en `data/out/consolidado-<categoria>-YYYYMMDD.xlsx`.
   - Hace spot-check (sección "Verificación").
   - Reporta: cuántas filas procesadas, cuántas vacías/descartadas, qué archivos requirieron iLovePDF, qué casos sospechosos.

4. **Salo revisa el consolidado**, pide ajustes finos (descartar filas, recotizar con flete distinto, etc.), y se cierra esa categoría.

---

## Columnas finales del consolidado (las 20 estándar)

Orden exacto, definido en `scripts/cotizar_xlsx.py:30-35` y `scripts/enrich_xlsx.py:38-43`:

```
1.  Foto                  (imagen embebida)
2.  Proveedor
3.  Descripción
4.  SKU
5.  Material              (gatilla tasa 35% si "steel"/"acero")
6.  Medidas
7.  Peso
8.  Pzas/Caja
9.  CBM/Caja              (derivado si vacío: parse "CTN: LxWxH cm")
10. Pzas/Contenedor       (derivado: floor((CBM_40HQ / CBM_caja) × Pzas_caja))
11. MOQ
12. Lead time
13. FOB USD               (read-only del proveedor, nunca inventado)
14. Landed MXN/pza        (calculado por motor 14 pasos)
15. Retail c/IVA          (calculado por motor 14 pasos)
16. Fracción              (lookup_tariff(cat, subcat) si vacío)
17. Tasa %                (25 default, 35 si steel, o lookup_tariff)
18. Categoría
19. Sub-categoría
20. Archivo origen
```

**Regla inviolable:** si una columna no está en el archivo origen y no es derivable, queda vacía. Cero inventos.

---

## Manejo de PDFs

Hay tres caminos. La elección depende del PDF.

**Camino A — Parseo directo (default):**
- `src/parsers/pdf_parser.py` con `pdfplumber.extract_tables()`.
- Funciona bien cuando el PDF es **tabla nativa** (no escaneado, no catálogo de marketing).
- Cubre ~70-80% de los PDFs de proveedor estándar.

**Camino B — Vision (fallback automático):**
- Si `pdfplumber` no encuentra tablas, se renderiza la página con `pypdfium2` a 150 DPI y se manda a Claude Haiku 4.5 con tool `record_quotes`.
- Activado con `VISION_PDF=1` en env.
- Caro pero útil para catálogos / escaneados.

**Camino C — iLovePDF / Adobe externo (manual, recomendado para PDFs "bonitos"):**
- Cuando el PDF tiene fotos por fila y el alineamiento foto↔fila importa (típico catálogo Alibaba con foto pequeña al lado del SKU).
- Salo convierte manualmente con iLovePDF Pro o Adobe Acrobat → XLSX.
- El XLSX resultante entra al pipeline normal de XLSX (`src/parsers/xlsx_parser.py`), que sí mantiene foto-fila alineada por drawings.

**Heurística para decidir:**
- ¿El PDF es escaneado o tiene layout de catálogo con fotos visibles? → **Camino C** (iLovePDF antes de pasar a Claude).
- ¿El PDF tiene tablas planas tipo cotización Excel impresa a PDF? → **Camino A**, y si falla, **B**.
- En el reporte por categoría, Claude debe listar explícitamente qué PDFs **falló parsear bien** y sugerir cuáles pasar por iLovePDF para reintentar.

---

## Archivos multi-categoría

Tres opciones soportadas (de mejor a peor):

**Opción 1 — Duplicar fila en el índice (preferido):**
- Acowork (o Salo) duplica el archivo en el índice, una fila por categoría.
- Cada fila tiene en `subset_skus` los SKUs/descripciones específicos de esa categoría, separados por `;`.
- Claude parsea el archivo completo, luego filtra los rows cuyo `sku` o `description` (case-insensitive contains) caigan en `subset_skus`.

**Opción 2 — Marcado fila-por-fila después del primer parseo:**
- Claude parsea el archivo a una "vista previa" en `data/preview/<archivo>.xlsx` con todas las filas y un id por fila.
- Salo marca con `X` en columna `mantener` solo las filas relevantes a la categoría actual.
- Claude regenera el consolidado solo con esas filas.
- Útil cuando Salo no sabe de antemano qué SKUs quiere.

**Opción 3 — Filtro por keywords/categoría auto (último recurso):**
- Claude usa el clasificador existente (`category` y `subcategory` en `parsed_quotes`) para filtrar rows que matcheen la categoría activa.
- Riesgo de falsos positivos/negativos. Solo para volumen alto y cuando 1 y 2 no son prácticas.

**Por default usar Opción 1.** Si el archivo es muy heterogéneo (catálogo gigante), Opción 2.

---

## Los 39 sin clasificar

Antes de empezar el loop por categoría, una sesión dedicada de triage:

1. Claude lista los 39 con: nombre, proveedor, fecha, primer screenshot/preview de 1 página.
2. Salo asigna categoría existente, crea categoría nueva, o marca `descartar`.
3. Claude actualiza `categoria` en el índice y los archivos descartados pasan a `data/discarded/`.
4. Después de eso, esos archivos entran al flujo normal por categoría.

No procesar los 39 hasta haber hecho este triage — meterlos al loop sin categoría asignada los hace caer al consolidado equivocado.

---

## Cotización: parámetros default

Definidos en `src/cotizador/engine.py` (motor 14 pasos). Defaults aplicados por `scripts/cotizar_xlsx.py`:

| Parámetro | Default | Override |
|---|---|---|
| TC USD/MXN | **20.00** (defensivo, Banxico cancelado) | `--tc 20.5` |
| Flete marítimo USD/40HQ | 5000 | `--flete-maritimo-usd 5500` |
| Flete local MXN/contenedor | 70000 | `--flete-local-mxn 75000` |
| CBM 40HQ utilizable | 67 | `--cbm-40hq 65` |
| Tasa arancelaria default | 25% | auto 35% si Material contiene "steel"/"acero", o lookup_tariff |
| IVA | 16% | (no override) |
| Margen retail | según motor | (no override en CLI) |

**Antes de cotizar una categoría nueva**, confirmar con Salo si esa categoría tiene fracción/tasa específica conocida (ej. mascotas con fracción 9508 vs herrajes con 7326). El motor lookup_tariff cubre lo común; lo raro requiere override.

---

## Clasificación: taxonomía actual

Las categorías y subcategorías viven en `src/cotizador/tariffs.py` (lookup_tariff) y se asignan por keywords + LLM fallback en `src/parsers/`. Antes de procesar una categoría nueva del índice, verificar que el clasificador la reconozca; si no, agregar keywords al diccionario o aceptar que `category`/`subcategory` queden vacías y se rellenen manualmente.

Categorías ya soportadas (consultar lista actual con `sqlite3 alibaba.db "SELECT DISTINCT category FROM parsed_quotes WHERE category IS NOT NULL"`).

---

## Convenciones de salida

```
data/
  indices/
    acowork-YYYYMMDD.xlsx                      # índice maestro
    seleccion-<categoria>-YYYYMMDD.xlsx        # subset marcado por Salo
  inbox/
    <categoria>-YYYYMMDD/                      # archivos descargados/copiados
  preview/
    <archivo_origen>.preview.xlsx              # vista previa cuando aplique opción 2 multi-cat
  out/
    consolidado-<categoria>-YYYYMMDD.xlsx      # ENTREGA FINAL — 20 cols, fotos embebidas, cotizado
  discarded/
    <archivo>.xlsx                             # los que Salo descartó del triage
```

Nombre del consolidado final SIEMPRE con fecha del día en que se generó, no fecha del correo origen.

---

## Verificación (spot-check antes de entregar)

Claude debe correr y reportar antes de declarar listo el consolidado:

1. **Conteo:** N filas marcadas en selección → N′ archivos parseados → N″ filas en consolidado. Si N″ << esperado, investigar por qué (parser falló, multi-cat filter muy estricto, etc.).
2. **Foto-fila alineada:** spot-check en 3 filas random — la foto corresponde al SKU/descripción de la misma fila. Si no, ese archivo va por iLovePDF.
3. **FOB USD nunca inventado:** ninguna fila tiene FOB que no estuviera en archivo origen. Cualquier fila con FOB calculado/inferido se marca y se reporta.
4. **Landed MXN > FOB×TC:** sanity check — Landed siempre mayor que FOB en MXN puro (fletes + arancel + IVA + margen lo aseguran). Si Landed < FOB×TC, algo se rompió en el motor.
5. **CBM/caja parsea:** si una fila tiene Pzas/Caja pero no CBM/Caja después del enrich, hay un parse fallido en `Medidas` — reportarlo.
6. **Listado de archivos problemáticos** que requirieron fallback vision o que sugieres reprocesar con iLovePDF.

---

## Comandos útiles (referencia rápida)

```bash
# Parsear una carpeta a parsed_quotes y consolidado crudo
python scripts/parse_folder.py data/inbox/<categoria>-YYYYMMDD/

# Enriquecer columnas derivables (CBM, pzas/contenedor, fracción, tasa)
python scripts/enrich_xlsx.py data/out/<archivo>.xlsx

# Calcular Landed + Retail
python scripts/cotizar_xlsx.py data/out/<archivo>.xlsx --tc 20 --flete-maritimo-usd 5000 --flete-local-mxn 70000

# Reparsear toda la DB (idempotente, útil tras cambios de parser)
python scripts/reparse_chat_files.py

# Activar vision para PDFs sin tabla
VISION_PDF=1 python scripts/parse_folder.py data/inbox/<categoria>-YYYYMMDD/

# Ver categorías existentes
sqlite3 alibaba.db "SELECT category, COUNT(*) FROM parsed_quotes GROUP BY category ORDER BY 2 DESC"

# Ver clusters cross-supplier (oportunidades de comparación)
sqlite3 alibaba.db "SELECT cluster_id, COUNT(DISTINCT supplier_name) AS n_sup FROM parsed_quotes WHERE cluster_id IS NOT NULL GROUP BY cluster_id HAVING n_sup > 1"
```

---

## Resumen ejecutivo del loop

```
[Triage 39 sin clasificar]  ─→ asigna categoria o discard
        │
        ▼
[Salo marca categoría X en índice]  ─→ seleccion-X-YYYYMMDD.xlsx
        │
        ▼
[Claude descarga + parsea archivos marcados]
        │
        ├─ XLSX/XLS  → xlsx_parser
        ├─ PDF tabla → pdf_parser (camino A)
        ├─ PDF caos  → vision (camino B) o iLovePDF manual (camino C)
        └─ Imágenes  → vision_parser
        │
        ▼
[Filtro multi-categoría si aplica]  (subset_skus o marcado fila)
        │
        ▼
[Enrich → Cotizar 14 pasos]
        │
        ▼
[Spot-check: foto-fila, FOB no inventado, sanity Landed]
        │
        ▼
[Entrega: data/out/consolidado-X-YYYYMMDD.xlsx]
        │
        ▼
[Salo revisa, pide ajustes finos, cierra categoría]
        │
        ▼
[Siguiente categoría → repeat]
```

---

## Lo que NO hace este plan (explícito)

- No procesa todo el índice de un jalón — siempre por categoría marcada.
- No inventa FOB, MOQ, ni medidas faltantes.
- No envía nada a proveedores ni manda correos automáticos.
- No re-cotiza categorías ya cerradas a menos que Salo lo pida explícitamente.
- No modifica el índice maestro de Acowork — solo lee y exporta subsets.

---

## Archivos críticos del repo (para que el agente en la otra compu sepa dónde tocar)

- `scripts/parse_folder.py` — entry point bulk parsing
- `scripts/enrich_xlsx.py` — derivaciones deterministas
- `scripts/cotizar_xlsx.py` — motor 14 pasos a XLSX
- `scripts/build_formato_hd_alimentadores.py` + `sort_*` — formato HD destino (si aplica esa categoría)
- `src/parsers/__init__.py` — dispatcher por extensión
- `src/parsers/pdf_parser.py` — pdfplumber + vision fallback
- `src/parsers/xlsx_parser.py` — xlsx con drawings/imágenes embebidas
- `src/parsers/vision_parser.py` — Claude Haiku 4.5
- `src/cotizador/engine.py` + `tariffs.py` — motor 14 pasos + lookup
- `src/db.py` (líneas 336-411) — schema parsed_quotes
- `docs/AGENTE_OPERADOR.md` — reglas del operador (cero inventos, no auto-reply)
