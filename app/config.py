"""Configuración leída de variables de entorno (o archivo .env)."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


TZ = ZoneInfo("America/Argentina/Buenos_Aires")

SECRET_KEY = _get("SECRET_KEY", "dev-no-usar-en-produccion")
ADMIN_NOMBRE = _get("ADMIN_NOMBRE", "Vengil")
ADMIN_PIN = _get("ADMIN_PIN", "1234")
SESION_HORAS = int(_get("SESION_HORAS", "12"))

_db = _get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'parka_deposito.db'}")
if _db.startswith("postgres://"):
    _db = "postgresql+psycopg://" + _db[len("postgres://"):]
elif _db.startswith("postgresql://"):
    _db = "postgresql+psycopg://" + _db[len("postgresql://"):]
DATABASE_URL = _db

# Cierre propio por SMTP (apagado por defecto: el mail lo manda Google)
CIERRE_AUTOMATICO = _get("CIERRE_AUTOMATICO", "0") == "1"
CIERRE_HORA = _get("CIERRE_HORA", "19:00")
CIERRE_DIAS = _get("CIERRE_DIAS", "mon-sat")
MAIL_TO = _get("MAIL_TO", "gerencia@magontex.com.ar")
MAIL_FROM = _get("MAIL_FROM", "")
SMTP_HOST = _get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(_get("SMTP_PORT", "587"))
SMTP_USER = _get("SMTP_USER", "")
SMTP_PASS = _get("SMTP_PASS", "")

# Google Drive / Apps Script "PARKA Despacho Backend v3" (el mismo del scanner)
GAS_URL = _get("GAS_URL", "https://script.google.com/macros/s/AKfycbwP_qnW67-sO-EMCyZSVStCkRXCXtNT7mDE-l-z6vqGFbmZVumhMf7KBTxMiXfZeu-X/exec")
# Minutos antes de la hora de cierre de Google en que se informan los pendientes del día
GAS_MINUTOS_ANTES = int(_get("GAS_MINUTOS_ANTES", "10"))
GAS_AUTO = _get("GAS_AUTO", "1") == "1"

# Apps Script "Devoluciones ML — Backend (Google Drive)": crea Crédito y Reingreso en Drive
DEVOL_URL = _get("DEVOL_URL", "https://script.google.com/macros/s/AKfycbxzFr7gjMFHjLlGsY0OEclD7-S6yqziMomw6MkIJXMBnx6ZajueyOFrxd8W9ZhvUQM3/exec")
# Calibración de la impresora para las hojas A4 autoadhesivas 4,8 x 2,5 (mm)
ETIQUETAS_DX = float(_get("ETIQUETAS_DX", "1.0"))
ETIQUETAS_DY = float(_get("ETIQUETAS_DY", "0"))

PIN_MAX_INTENTOS = 5
PIN_BLOQUEO_MIN = 5
