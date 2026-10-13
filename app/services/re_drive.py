"""Drive de Recolección Ecommerce: Apps Script propio ("Recolección Ecommerce — Backend Drive").

  GET  ?token&action=estado                  → versión y carpeta raíz
  GET  ?token&action=hoy                     → crea (si falta) la carpeta de hoy y lista sus archivos
  GET  ?token&action=listar&fecha=aaaa-mm-dd → archivos de esa fecha (sin crear la carpeta)
  GET  ?token&action=archivo&id=…            → archivo en base64 (las Hojas de Google vienen como .xlsx)
  POST {token, action:"guardar", fecha, nombre, base64, mime} → guarda en la carpeta de la fecha
Respuesta: {"ok": true, "data": …} o {"ok": false, "error": "…"}.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request

from sqlalchemy.orm import Session

from . import ajustes


class REDriveError(Exception):
    pass


def _cfg(db: Session | None) -> tuple[str, str]:
    url, token = ajustes.leer("re_url", db).strip(), ajustes.leer("re_token", db).strip()
    if not url or not token:
        raise REDriveError("Falta conectar el Apps Script de Recolección Ecommerce (Conexiones → URL y TOKEN)")
    return url, token


def configurado(db: Session | None = None) -> bool:
    try:
        _cfg(db)
        return True
    except REDriveError:
        return False


def _leer(req, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        raise REDriveError(f"Sin conexión con Google Drive ({e})") from e
    try:
        data = json.loads(txt)
    except ValueError as e:
        raise REDriveError("El Apps Script no devolvió JSON: revisá que esté implementado como "
                           "'Aplicación web' con acceso 'Cualquier persona'") from e
    if not isinstance(data, dict) or not data.get("ok"):
        raise REDriveError(str((data or {}).get("error") or "Respuesta inválida del Apps Script")[:300])
    return data.get("data") or {}


def _get(db: Session | None, action: str, timeout: int = 60, **params) -> dict:
    url, token = _cfg(db)
    qs = urllib.parse.urlencode({"action": action, "token": token, **params})
    return _leer(urllib.request.Request(f"{url}?{qs}"), timeout)


def _post(db: Session | None, action: str, timeout: int = 120, **body) -> dict:
    url, token = _cfg(db)
    datos = json.dumps({"action": action, "token": token, **body}).encode("utf-8")
    req = urllib.request.Request(url, data=datos, method="POST", headers={"Content-Type": "text/plain;charset=utf-8"})
    return _leer(req, timeout)


def estado(db: Session | None = None) -> dict:
    return _get(db, "estado", timeout=30)


def hoy(db: Session | None = None) -> dict:
    """{"fecha", "id", "nombre", "url", "archivos": [{id, nombre, tipo, mime, bytes, modificado}]}"""
    return _get(db, "hoy", timeout=60)


def listar(db: Session | None, fecha: str) -> dict:
    return _get(db, "listar", timeout=60, fecha=fecha)


def archivo(db: Session | None, file_id: str) -> tuple[str, bytes]:
    d = _get(db, "archivo", timeout=120, id=file_id)
    return d.get("nombre") or "archivo", base64.b64decode(d.get("base64") or "")


def guardar(db: Session | None, fecha: str, nombre: str, contenido: bytes,
            mime: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") -> dict:
    return _post(db, "guardar", fecha=fecha, nombre=nombre, mime=mime,
                 base64=base64.b64encode(contenido).decode("ascii"))
