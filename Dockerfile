FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencias del sistema:
#  - curl: healthcheck.
#  - libreoffice-calc: convierte el template HD .xlsb -> .xlsx en Linux
#    (openpyxl no lee .xlsb). La exportacion HD lo usa via app/formato_hd.py.
#    En Windows no aplica (ese entorno usa Excel COM).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libreoffice-calc \
    && rm -rf /var/lib/apt/lists/*

# Instalación de dependencias Python
# pywin32 está marcado con `sys_platform == 'win32'` en pyproject, se omite en Linux.
COPY pyproject.toml ./
COPY app/ ./app/
RUN pip install -e ".[dev]"

# El resto del código se monta en runtime con docker-compose (volumen),
# pero también lo copiamos para que la imagen sea ejecutable sin volumen.
COPY . .

EXPOSE 8081

# Healthcheck simple contra la raíz de la UI
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8081/ > /dev/null || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081"]
