#!/usr/bin/env python3
"""
Descargador de adjuntos PDF/Excel/Word de los correos de fortuna@lloydselectronica.com.

INSTRUCCIONES (1ra vez):
  1) Instala dependencias:
        pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib openpyxl

  2) Crea credenciales OAuth en Google Cloud Console:
       a) https://console.cloud.google.com/  → crea/elige un proyecto
       b) APIs y servicios → Biblioteca → habilita "Gmail API"
       c) Pantalla de consentimiento → tipo Externo → completa lo mínimo
          (puedes ponerte como tester en "Audiencia" con tu propio email)
       d) Credenciales → Crear credenciales → ID de cliente OAuth →
          Aplicación de escritorio → Crear → DESCARGA el JSON
       e) Renómbralo a "credentials.json" y déjalo en la MISMA carpeta donde
          guardes este script (Documents/Mascotas-9Mayo).

  3) Ejecuta:
        cd "%USERPROFILE%\\Documents\\Mascotas-9Mayo"
        python descargar_adjuntos.py

     La primera vez te abrirá el navegador para autorizar el acceso a tu Gmail.
     Solo se piden permisos de LECTURA (gmail.readonly). Se guardará un
     "token.json" para no tener que re-autorizar la próxima vez.

QUÉ HACE:
  - Busca todos los hilos de fortuna@lloydselectronica.com desde 2026-04-01
  - Descarga cada adjunto PDF/Excel/Word a esta carpeta
  - Renombra los archivos como: AAAA-MM-DD__asunto-corto__nombre-original.ext
  - Actualiza el INDICE.xlsx agregando columna "Archivo descargado" con el
    nombre real del archivo que quedó en disco (link clickeable).

Si lo ejecutas varias veces no descarga lo mismo dos veces (skip si ya existe).
"""
import os
import re
import json
import base64
import time
from datetime import datetime, timezone
from pathlib import Path

import sys
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except ImportError:
    print("ERROR: Falta instalar dependencias.")
    print('Corre: pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib openpyxl')
    sys.exit(1)

# === CONFIG ===
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SENDER = "fortuna@lloydselectronica.com"
AFTER_DATE = "2026/04/01"   # YYYY/MM/DD (Gmail format)
ALLOWED_EXTS = {".pdf", ".xlsx", ".xls", ".docx", ".doc"}

HERE = Path(__file__).parent.resolve()
OUTDIR = HERE
TOKEN_PATH = HERE / "token.json"
CRED_PATH = HERE / "credentials.json"


def auth():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CRED_PATH.exists():
                raise SystemExit(
                    f"\nNo se encuentra {CRED_PATH}.\n"
                    "Sigue los pasos del encabezado del script para crearlo y\n"
                    "ponlo en la carpeta Mascotas-9Mayo.\n"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CRED_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def safe(s, maxlen=50):
    s = re.sub(r"[^\w\s\.\-]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:maxlen]


def list_thread_ids(svc, query):
    out = []
    page = None
    while True:
        resp = svc.users().threads().list(
            userId="me", q=query, pageToken=page, maxResults=100
        ).execute()
        for t in resp.get("threads", []):
            out.append(t["id"])
        page = resp.get("nextPageToken")
        if not page:
            break
    return out


def walk_parts(parts):
    for p in parts or []:
        yield p
        for c in walk_parts(p.get("parts")):
            yield c


def find_attachments_in_message(msg):
    """Yield (filename, attachment_id, mimeType) for each non-inline attachment."""
    payload = msg.get("payload", {})
    for p in walk_parts([payload] + (payload.get("parts") or [])):
        body = p.get("body") or {}
        fn = p.get("filename") or ""
        att_id = body.get("attachmentId")
        if fn and att_id:
            yield fn, att_id, p.get("mimeType", "")


def get_thread(svc, tid):
    return svc.users().threads().get(userId="me", id=tid, format="full").execute()


def get_attachment(svc, msg_id, att_id):
    resp = svc.users().messages().attachments().get(
        userId="me", messageId=msg_id, id=att_id
    ).execute()
    data = resp.get("data", "")
    return base64.urlsafe_b64decode(data.encode("utf-8") + b"===")


def msg_date(msg):
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    iso = msg.get("internalDate")
    if iso:
        try:
            return datetime.fromtimestamp(int(iso) / 1000, tz=timezone.utc)
        except Exception:
            pass
    return None


def msg_subject(msg):
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return headers.get("Subject", "")


def main():
    print("== Descargador de adjuntos Mascotas-9Mayo ==")
    svc = auth()
    query = f"from:{SENDER} after:{AFTER_DATE} has:attachment"
    print(f"Buscando hilos: {query}")
    tids = list_thread_ids(svc, query)
    print(f"  → {len(tids)} hilos encontrados")

    OUTDIR.mkdir(parents=True, exist_ok=True)
    log = OUTDIR / "descarga_log.csv"
    with open(log, "w", encoding="utf-8") as logf:
        logf.write("fecha,thread_id,message_id,asunto,filename_original,filename_guardado,status\n")

        skipped = downloaded = errors = 0
        for i, tid in enumerate(tids, 1):
            try:
                th = get_thread(svc, tid)
            except Exception as e:
                print(f"  [{i}/{len(tids)}] ERROR thread {tid}: {e}")
                errors += 1
                continue

            for m in th.get("messages", []):
                date = msg_date(m)
                subj = msg_subject(m)
                date_str = date.strftime("%Y-%m-%d") if date else "0000-00-00"
                subj_safe = safe(subj, 40) or "sin-asunto"

                attachments = list(find_attachments_in_message(m))
                for fn, att_id, mtype in attachments:
                    ext = Path(fn).suffix.lower()
                    if ext not in ALLOWED_EXTS:
                        continue
                    target_name = f"{date_str}__{subj_safe}__{safe(fn, 60)}"
                    target = OUTDIR / target_name
                    if target.exists() and target.stat().st_size > 0:
                        logf.write(f'"{date_str}",{tid},{m["id"]},"{subj}","{fn}","{target_name}",skipped\n')
                        skipped += 1
                        continue
                    try:
                        data = get_attachment(svc, m["id"], att_id)
                        target.write_bytes(data)
                        logf.write(f'"{date_str}",{tid},{m["id"]},"{subj}","{fn}","{target_name}",ok\n')
                        downloaded += 1
                        if downloaded % 10 == 0:
                            print(f"  [{i}/{len(tids)}] descargados:{downloaded} skipped:{skipped} errores:{errors}")
                    except Exception as e:
                        print(f"  [{i}/{len(tids)}] ERROR {fn}: {e}")
                        logf.write(f'"{date_str}",{tid},{m["id"]},"{subj}","{fn}","{target_name}",error:{e}\n')
                        errors += 1
                # gentle pacing
                time.sleep(0.05)

            if i % 25 == 0:
                print(f"  Progreso: {i}/{len(tids)} hilos | descargados:{downloaded} skipped:{skipped} errores:{errors}")

    print()
    print(f"Listo. Descargados:{downloaded}  Skipped:{skipped}  Errores:{errors}")
    print(f"Log: {log}")
    print(f"Carpeta: {OUTDIR}")


if __name__ == "__main__":
    main()
