# Plan: Export HD vía cola distribuida con Resilio Sync

## Context

El container Docker (Linux) corre en un server central y sirve la UI/BD. Los usuarios finales tienen Windows con Excel + Resilio Sync. La función "Exportar marcados" necesita Excel COM (`pywin32`), que no existe en Linux.

**Idea clave:** la carpeta del proyecto **ya está compartida** vía Resilio Sync entre el server y los Windows de los usuarios. Podemos usar esa carpeta compartida como una **cola distribuida** de trabajos:
- El server escribe el `_intermedio_X.xlsx` en una subcarpeta de la share.
- Resilio lo replica a todos los Windows en segundos.
- Un **worker** corriendo en cualquier Windows del usuario lo detecta, lo procesa con Excel real, y devuelve el resultado por la misma vía.

**Outcome esperado:**
- Container Docker corre en cualquier OS sin necesitar Excel.
- Los usuarios siguen marcando productos y clickeando "Exportar" en la UI — el flujo se siente igual (con un spinner de ~30-60s).
- Cualquier Windows del equipo con el worker prendido puede procesar (2-5 usuarios). Si uno está apagado, otro lo recoge.

## Arquitectura

### Estructura de carpetas

```text
data/exportar/             ← ESTA carpeta es la única compartida vía Resilio
  ├── pendientes/          ← server drop aquí
  ├── procesando/          ← worker mueve cuando toma el job (lock)
  ├── listos/              ← worker drop el HD final aquí
  └── errores/             ← worker drop si falla, con .log

data/productos.db          ← BD: queda SOLO en el server, no se sincroniza
data/fotos/                ← Fotos: solo en server
```

### Cómo se comparte la carpeta entre containers (Patrón A)

El container Resilio Sync ya existe y monta una carpeta del host. El container `mascotas-bd` monta **la misma carpeta del host** vía bind mount. Ambos ven los mismos archivos — no se hablan directamente, el sistema de archivos del host es el punto de encuentro.

```text
┌──────────────────── Host (Ubuntu) ─────────────────────────────┐
│                                                                │
│  Path del host: ${RESILIO_SHARE_PATH}/cotizador-exportar/      │
│    ├── pendientes/                                             │
│    ├── procesando/                                             │
│    ├── listos/                                                 │
│    └── errores/                                                │
│         ▲                              ▲                       │
│         │ bind mount                   │ bind mount            │
│         │ /sync/cotizador-exportar     │ /app/data/exportar    │
│         │                              │                       │
│  ┌──────┴───────────────┐    ┌─────────┴───────────────────┐  │
│  │ resilio-sync         │    │ mascotas-bd                 │  │
│  │ (ya existe)          │    │ (app FastAPI)               │  │
│  │ sincroniza con       │    │ código en ./:/app           │  │
│  │ Windows del equipo   │    │ data en host bind           │  │
│  └──────────────────────┘    └─────────────────────────────┘  │
│                                                                │
└────────────────────────────────────────────────────────────────┘
                  ▲
                  │ Resilio Sync replica via internet/LAN
                  ▼
┌──────────── Windows usuario A,B,C ────────────────────────────┐
│  D:\Resilio-Sync\...\cotizador-exportar\                      │
│    ├── pendientes/  ← worker poll cada 5s                     │
│    ├── procesando/                                            │
│    ├── listos/                                                │
│    └── errores/                                               │
│                                                               │
│  exportador_worker.py (corre fuera de Docker)                 │
│  llenar_formato_hd.py                                         │
│  Formato HD-Mascotas.xlsb                                     │
└───────────────────────────────────────────────────────────────┘
```

### Flujo de un export

```text
┌─────────────────────────┐                ┌──────────────────────────────┐
│ Server (Docker, Linux)  │                │  Windows del usuario A,B,C   │
│                         │                │  ─────────────────────────   │
│ POST /exportar          │                │  exportador_worker.py        │
│   ├─ genera intermedio  │   Resilio      │  (poll cada 5s)              │
│   ├─ mueve a            │   Sync         │                              │
│   │  data/exportar/     │ ◄────────────► │  rename a procesando/        │
│   │  pendientes/        │                │  (lock atómico)              │
│   └─ devuelve job_id    │                │      ↓                       │
│                         │                │  llenar_formato_hd.py        │
│ GET /exportar/status/   │                │  (Excel COM real)            │
│  {id} → polling         │                │      ↓                       │
│   ├─ chequea listos/    │                │  mueve a listos/             │
│   └─ devuelve URL       │                │                              │
│      cuando aparece     │                │                              │
└─────────────────────────┘                └──────────────────────────────┘
```

## Componentes

### Container side (modificaciones)

**1. Estructura de carpetas** — agregar al startup de `app/main.py`:

```python
for sub in ("pendientes", "procesando", "listos", "errores"):
    (Path(__file__).parent.parent / "data" / "exportar" / sub).mkdir(parents=True, exist_ok=True)
```

**2. `app/routes.py` — endpoint `_correr_llenar_formato_hd` (líneas 421-513)**

Reemplazar el subprocess local por: **escribir intermedio en `pendientes/` y devolver job_id**. Convertir la endpoint en asincrona.

Nueva forma:

- `POST /exportar` (o el existente que use `_correr_llenar_formato_hd`):
  - Genera intermedio (código actual de `generar_formato_hd_desde_marcados` / `generar_formato_hd_por_categoria` — sin cambios).
  - Construye `job_id = stem del intermedio sin "_intermedio_"` (usar la misma lógica de routes.py:472-475).
  - Mueve el intermedio a `data/exportar/pendientes/_intermedio_<job_id>.xlsx`.
  - Devuelve JSON: `{"job_id": "<id>", "status_url": "/exportar/status/<id>"}`.

- `GET /exportar/status/{job_id}` — nuevo endpoint:
  - Si existe `data/exportar/listos/formato-hd-<job_id>.xlsx` → `{"status": "ready", "download": "/exportar/download/<job_id>"}`.
  - Si existe `data/exportar/errores/_intermedio_<job_id>.xlsx.log` → `{"status": "error", "message": "<contenido log truncado>"}`.
  - Si está en `procesando/` → `{"status": "processing", "claimed_by": "<hostname>"}` (leer del nombre del archivo).
  - Si sigue en `pendientes/` → `{"status": "queued"}`.
  - Si no aparece en ninguna → `{"status": "unknown"}`.

- `GET /exportar/download/{job_id}` — nuevo endpoint:
  - Devuelve `FileResponse` del archivo en `listos/`. Después de descargar, opcionalmente moverlo a un archivo de histórico (o dejarlo, Resilio no lo borrará).

**3. UI (`app/templates/`) — JS de polling**

En el botón "Exportar marcados" cambiar el handler (Alpine.js) para:

```js
async function exportar() {
    const resp = await fetch('/exportar', {method:'POST', ...});
    const {job_id, status_url} = await resp.json();
    showSpinner('Esperando worker...');
    while (true) {
        await sleep(2000);
        const s = await fetch(status_url).then(r => r.json());
        if (s.status === 'ready') {
            window.location = `/exportar/download/${job_id}`;
            break;
        }
        if (s.status === 'error') {
            showError(s.message);
            break;
        }
        updateSpinner(s.status); // 'queued', 'processing'
    }
}
```

Con timeout de 5 min y mensajes claros ("Si tarda más de 30s, verifica que algún worker esté corriendo").

### Worker side (nuevo)

**Archivo nuevo: `exportador_worker.py`**

Importante (con Patrón A): los scripts del worker (`exportador_worker.py`, `llenar_formato_hd.py`, `Formato HD-Mascotas.xlsb`) **NO viven en Resilio** — son archivos locales en cada Windows. **Solo** `data/exportar/` se sincroniza vía Resilio. Por eso el worker necesita DOS paths configurables:

- `WORKER_ROOT` → carpeta local donde están los scripts del worker (ej. `C:\cotizador-worker\`).
- `EXPORTAR_DIR` → carpeta que Resilio sincroniza (ej. `D:\Resilio-Sync\cotizador-exportar\`).

Ambos se leen de variables de entorno o de un `.env` local al worker.

Estructura (~90 líneas):

```python
"""
Worker que procesa exports HD desde Resilio Sync.

Uso: python exportador_worker.py
Detiene con Ctrl+C.

Setup en Windows:
  1. Clonar repo (o copiar) a una carpeta local, ej. C:\cotizador-worker\
  2. Tener Resilio Sync sincronizando la carpeta de exportar
     (la share key te la pasa el admin del server).
  3. Crear .env junto al worker con:
        WORKER_ROOT=C:\cotizador-worker
        EXPORTAR_DIR=D:\Resilio-Sync\cotizador-exportar
  4. pip install pywin32 openpyxl pillow python-dotenv
  5. python exportador_worker.py
  6. (Opcional) crear tarea programada en Windows para arrancar al login.
"""
import os, sys, time, socket, subprocess, shutil
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # lee .env al lado del script

WORKER_ROOT = Path(os.environ.get("WORKER_ROOT", Path(__file__).parent)).resolve()
EXPORTAR_DIR = Path(os.environ["EXPORTAR_DIR"]).resolve()  # obligatorio
PENDIENTES = EXPORTAR_DIR / "pendientes"
PROCESANDO = EXPORTAR_DIR / "procesando"
LISTOS = EXPORTAR_DIR / "listos"
ERRORES = EXPORTAR_DIR / "errores"
TEMPLATE = WORKER_ROOT / "Formato HD-Mascotas.xlsb"
SCRIPT = WORKER_ROOT / "llenar_formato_hd.py"

HOSTNAME = socket.gethostname()
POLL_INTERVAL = 5  # segundos
SYNC_WAIT = 5      # esperar a Resilio después de claim


def asegurar_dirs():
    for d in [PENDIENTES, PROCESANDO, LISTOS, ERRORES]:
        d.mkdir(parents=True, exist_ok=True)


def reclamar_archivo(pendiente: Path) -> Path | None:
    """Mueve atómicamente a procesando/ con sufijo de hostname. Si falla otro worker ganó."""
    claimed_name = f"{pendiente.name}.{HOSTNAME}.{os.getpid()}.processing"
    claimed = PROCESANDO / claimed_name
    try:
        pendiente.rename(claimed)
    except (FileNotFoundError, OSError):
        return None
    # Esperar a Resilio. Si otro worker pisó nuestro rename, abandonamos.
    time.sleep(SYNC_WAIT)
    if not claimed.exists():
        return None
    return claimed


def procesar(claimed: Path) -> bool:
    # Quitar el sufijo .HOSTNAME.PID.processing para sacar el nombre original
    original_name = claimed.name.split(".processing")[0].rsplit(".", 2)[0]
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         str(claimed), str(TEMPLATE),
         "--mapeo", "C=8,O=11,P=16,Q=17", "--yes"],
        capture_output=True, text=True, cwd=str(WORKER_ROOT),
    )
    if result.returncode != 0:
        log = ERRORES / f"{original_name}.log"
        log.write_text(f"hostname: {HOSTNAME}\n\nSTDERR:\n{result.stderr}\n\nSTDOUT:\n{result.stdout}")
        claimed.rename(ERRORES / original_name)
        return False
    base = original_name.replace("_intermedio_", "").lower().replace(".xlsx", "")
    out_name = f"formato-hd-{base}.xlsx"
    # llenar_formato_hd.py escribe el output en cwd (WORKER_ROOT)
    out_path = WORKER_ROOT / out_name
    if not out_path.exists():
        log = ERRORES / f"{original_name}.log"
        log.write_text(f"No encontré output esperado: {out_name}")
        claimed.rename(ERRORES / original_name)
        return False
    shutil.move(out_path, LISTOS / out_name)
    claimed.unlink()
    return True


def main():
    asegurar_dirs()
    print(f"[{HOSTNAME}] Worker iniciado. Polling {PENDIENTES} cada {POLL_INTERVAL}s")
    while True:
        try:
            for f in sorted(PENDIENTES.glob("_intermedio_*.xlsx")):
                claimed = reclamar_archivo(f)
                if claimed is None:
                    continue
                print(f"[{HOSTNAME}] Procesando: {claimed.name}")
                ok = procesar(claimed)
                print(f"[{HOSTNAME}] {'OK' if ok else 'ERROR'}: {claimed.name}")
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("Worker detenido por usuario.")
            return
        except Exception as e:
            print(f"[{HOSTNAME}] Error en loop: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
```

## Manejo de carreras (2-5 workers)

El lock es por `rename` atómico + verificación post-Resilio:

1. Worker A y B ven el mismo archivo en `pendientes/`.
2. Ambos llaman `rename` casi simultáneo. **Localmente**, ambos `rename` pueden tener éxito (cada uno en su Windows ve la share antes del sync).
3. Resilio resuelve el conflicto: gana el primero que sincronizó al server. El otro queda con un nombre alternativo (`procesando/_intermedio_X.xlsx.conflicto-...`).
4. Después de `SYNC_WAIT=5s`, cada worker verifica que **su** archivo claimado (con su hostname/pid) sigue existiendo. Si no, abandona.

Esto NO es transaccional pero para 2-5 usuarios con exports esporádicos es robusto (probabilidad de carrera real < 1%). El peor caso es un archivo duplicado en `errores/` o `listos/` — no destructivo.

## Configuración de Resilio Sync e integración con el container existente

### En el server (donde corre Docker)

El container `resilio-sync` ya existe y monta una carpeta del host (ej. `/srv/resilio/`). Hay que crear una **nueva share** dedicada a la cola de export — recomendado **no mezclarla** con shares preexistentes:

1. Crear la carpeta en el host:
   ```bash
   sudo mkdir -p /srv/resilio/cotizador-exportar/{pendientes,procesando,listos,errores}
   sudo chown -R 1000:1000 /srv/resilio/cotizador-exportar  # mismo UID que el container mascotas-bd
   ```

2. En la UI de Resilio Sync (o por API), agregar `/srv/resilio/cotizador-exportar/` como nueva share **read+write**. Generar la share key y guardarla.

3. **NO** poner `*.xlsx` ni `*.log` en `.sync/IgnoreList`. Verificar también que el archivo `.sync/IgnoreList` global no los excluya.

4. Si el container `resilio-sync` ya tiene un bind mount del padre (ej. `/srv/resilio:/sync`), el nuevo folder es automáticamente visible. Si monta paths específicos, agregar:
   ```yaml
   resilio-sync:
     volumes:
       - /srv/resilio/cotizador-exportar:/mnt/sync/folders/cotizador-exportar
   ```

### En el container `mascotas-bd`

Agregar al `docker-compose.yml` el bind mount al **mismo path del host** que ve resilio-sync:

```yaml
services:
  mascotas-bd:
    # ... (config existente)
    volumes:
      - .:/app
      - /app/app.egg-info
      - ${RESILIO_SHARE_PATH}/cotizador-exportar:/app/data/exportar  # ⭐ NUEVO
    environment:
      - PYTHONUNBUFFERED=1
    env_file:
      - .env
```

En `.env` definir:
```
RESILIO_SHARE_PATH=/srv/resilio
```

Validación: `docker compose exec mascotas-bd ls -la /app/data/exportar` debe mostrar las 4 subcarpetas (`pendientes`, `procesando`, `listos`, `errores`).

### En cada Windows del usuario (worker)

1. Instalar **Resilio Sync** (cliente nativo Windows, no container).
2. Click "Add folder" → pegar la share key → elegir destino local (ej. `D:\cotizador-exportar\`).
3. Esperar primera sincronización (la carpeta se crea con las 4 subcarpetas vacías).
4. Tener el repo del proyecto clonado en local (ej. `C:\cotizador-worker\`). Solo necesita estos archivos:
   - `exportador_worker.py`
   - `llenar_formato_hd.py`
   - `Formato HD-Mascotas.xlsb`
   - `requirements.txt` mínimo con `pywin32 openpyxl pillow python-dotenv`
5. Crear `.env` en `C:\cotizador-worker\`:
   ```
   WORKER_ROOT=C:\cotizador-worker
   EXPORTAR_DIR=D:\cotizador-exportar
   ```
6. `pip install -r requirements.txt` (o instalar deps manualmente).
7. `python exportador_worker.py` — debe imprimir el polling iniciado.
8. Opcional: tarea programada de Windows para arrancarlo al login.

### Por qué dos carpetas separadas en Windows

- `D:\cotizador-exportar\` → carpeta sincronizada por Resilio. Solo contiene `pendientes/`, `procesando/`, `listos/`, `errores/`. Esto evita que Resilio tenga que sincronizar megabytes de código Python y archivos `.git`.
- `C:\cotizador-worker\` → carpeta local con scripts. Se actualiza vía `git pull` o copia manual cuando hay nueva versión.

## Archivos a modificar / crear

| Archivo | Cambio |
|---------|--------|
| `exportador_worker.py` (nuevo, raíz) | Worker para correr en cada Windows |
| `app/main.py` | Crear las 4 subcarpetas al startup |
| `app/routes.py` (líneas 421-513 y endpoints alrededor) | Split en `POST /exportar`, `GET /exportar/status/{id}`, `GET /exportar/download/{id}` |
| `app/templates/productos.html` y similares | JS de polling con spinner |
| `docker-compose.yml` | Sin cambios (el volumen `.:/app` ya cubre `data/exportar/`) |
| `README.md` | Documentar setup del worker |

## Reutilización (cosas que NO cambian)

- **`llenar_formato_hd.py` no se toca.** Sigue siendo el motor. El worker solo lo invoca por subprocess.
- **`generar_formato_hd_desde_marcados` y `generar_formato_hd_por_categoria` en `app/exportar.py`** quedan iguales (openpyxl puro, corren en container).
- El **cálculo del nombre de output** (routes.py:472-475) se reutiliza tanto en container (para predecir el job_id) como en worker (para encontrar el output del script).
- El **mapeo de columnas `C=8,O=11,P=16,Q=17`** sigue siendo el mismo.

## Verificación end-to-end

### Setup

1. En el server (donde corre Docker):

   ```bash
   docker compose up -d
   ```

2. En 1+ Windows del equipo:

   ```powershell
   pip install pywin32 openpyxl pillow
   python exportador_worker.py
   # → "[NOMBRE-PC] Worker iniciado. Polling D:\...\data\exportar\pendientes cada 5s"
   ```

### Prueba de happy path

1. Server: abrir UI, marcar 1-2 productos, click "Exportar marcados".
2. UI muestra: "Esperando worker..." → "Procesando en NOMBRE-PC..." → "Listo" → descarga automática.
3. Verificar `data/exportar/listos/formato-hd-X.xlsx` aparece y está bien formado (abrir en Excel).
4. Tiempo total esperado: 30-60s.

### Prueba de no-worker

1. Detener todos los workers.
2. Click "Exportar". UI debe quedarse en "En cola..." hasta timeout (5 min) y mostrar mensaje claro: "Ningún worker disponible. Verifica que `exportador_worker.py` esté corriendo en alguna PC."

### Prueba de error

1. Worker corriendo, pero **cerrar Excel manualmente** o corromper el template `Formato HD-Mascotas.xlsb` temporalmente.
2. Click "Exportar". UI debe mostrar status `error` con el stderr del script.

### Prueba de concurrencia (manual)

1. Iniciar 2 workers en 2 Windows.
2. Click "Exportar" 3 veces rápidas (3 jobs).
3. Cada worker debe procesar ~1-2. Verificar que no hay duplicados en `listos/` (solo 3 archivos, no 6).

## Trade-offs honestos

**Pros:**

- Excel real en Windows real → output idéntico al actual, sin riesgo de diferencias por LibreOffice/openpyxl.
- Workers redundantes: si uno se cae, otros toman.
- Server Docker queda 100% portable a cualquier OS, sin necesitar Excel ni puentes HTTP entre máquinas.
- Sin abrir puertos en las PCs de usuarios finales.
- Reusa el código actual de `llenar_formato_hd.py` sin modificación.

**Contras:**

- **Latencia agregada**: 5-30s por sincronización Resilio en cada dirección (intermedio out, output back). UX necesita spinner con texto.
- **Workers tienen que estar prendidos**: si nadie tiene su PC encendida con worker corriendo, los exports quedan en cola. Mitigación común: 1 PC "siempre prendida" (idealmente la del usuario más activo).
- **No es FIFO estricto**: si dos jobs entran a la vez y dos workers están libres, no hay garantía de orden. Para este caso de uso no importa.
- **Cambio de UX**: de respuesta sincronica (espera HTTP) a asíncrona (job_id + polling). UI debe rehacerse para mostrar progreso. Esto es trabajo de UI, no técnico.

## Plan de implementación incremental

Para minimizar riesgo, hacerlo en 3 fases:

**Fase 1** — Infraestructura, sin UI nueva:

- Crear estructura `data/exportar/{pendientes,procesando,listos,errores}/`.
- Implementar `exportador_worker.py`.
- Probar manualmente: dejar un `_intermedio_*.xlsx` en `pendientes/`, verificar que el worker lo procesa.

**Fase 2** — Container side endpoints:

- Modificar `_correr_llenar_formato_hd` para mover a `pendientes/` en vez de subprocess.
- Agregar `GET /exportar/status/{id}` y `/exportar/download/{id}`.
- Probar con `curl` desde el container.

**Fase 3** — UI con polling:

- Modificar template + JS para polling y spinner.
- Probar end-to-end desde el navegador.

Cada fase es independiente y verificable antes de pasar a la siguiente.
