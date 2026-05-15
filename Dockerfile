FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencias del sistema mínimas (openpyxl es puro Python, no requiere libs nativas)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
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
