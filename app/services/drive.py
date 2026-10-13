"""Conexión con Google Drive a través del Apps Script del scanner (PARKA Despacho Backend v3).

Se usan las mismas acciones que ya usaba la PWA:
  GET  listFiles?tipo=flex|colecta|tiendanube   → archivos del día en PARKA Despacho/<tipo>/<fecha>
  GET  getFile?fileId=…                         → contenido del archivo
  POST procesarExcel {data:{datos,fecha}, tipo} → guarda el .xlsx del despacho en Drive y lo anota en LogDia
  POST cerrarDia                                → arma el archivo único del día y lo manda por mail
  POST getConfig                                → mail de gerencia, copia y hora del cierre automático
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import date

from sqlalchemy import select, or_, and_
from sqlalchemy.orm import Session

from .. import config
from ..actividad import registrar
from ..db import hoy
from ..models import Lote, Paquete, Usuario, EnvioManual

log = logging.getLogger("parka.drive")


class DriveError(Exception):
    pass


def configurado() -> bool:
    return bool(config.GAS_URL)


def _leer(req, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        raise DriveError(f"Sin conexión con Google Drive ({e})") from e
    try:
        data = json.loads(txt)
    except ValueError as e:
        raise DriveError("El Apps Script no devolvió JSON: revisá que la implementación sea "
                         "'Aplicación web' con acceso 'Cualquier persona'") from e
    if isinstance(data, dict) and data.get("error"):
        raise DriveError(str(data["error"])[:300])
    return data


def gas_get(action: str, timeout: int = 60, **params) -> dict:
    if not configurado():
        raise DriveError("Falta GAS_URL en el archivo .env")
    qs = urllib.parse.urlencode({"action": action, **params})
    return _leer(urllib.request.Request(f"{config.GAS_URL}?{qs}"), timeout)


def gas_post(action: str, data=None, tipo: str | None = None, timeout: int = 120, **extra) -> dict:
    if not configurado():
        raise DriveError("Falta GAS_URL en el archivo .env")
    body = {"action": action, **extra}
    if data is not None:
        body["data"] = data
    if tipo:
        body["tipo"] = tipo
    req = urllib.request.Request(config.GAS_URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "text/plain;charset=utf-8"})
    return _leer(req, timeout)  # urllib sigue el redirect 302 de Google como GET (igual que fetch)


# ─────────────── Lectura de archivos del día ───────────────

def listar(tipo: str) -> dict:
    return gas_get("listFiles", tipo=tipo, timeout=90)


def obtener_archivo(file_id: str) -> tuple[str, bytes]:
    d = gas_get("getFile", fileId=file_id, timeout=120)
    nombre = d.get("name") or "archivo"
    if d.get("type") == "text":
        return nombre, (d.get("content") or "").encode("utf-8")
    return nombre, base64.b64decode(d.get("content") or "")


# ─────────────── Reporte de despachos a Drive ───────────────

def _items(paquetes: list[Paquete], estado: str) -> list[dict]:
    """Un renglón por venta con todos sus SKU juntos (igual que el scanner)."""
    grupos: "OrderedDict[str, dict]" = OrderedDict()
    for p in paquetes:
        k = p.venta_id or f"__pkg_{p.tracking}"
        g = grupos.setdefault(k, dict(venta=p.venta_id, comprador=p.comprador, trk=[], skus=[], cant=0))
        g["trk"].append(p.tracking)
        s = p.sku or p.producto
        if s and s not in g["skus"]:
            g["skus"].append(s)
        g["cant"] += p.cantidad or 1
        if not g["comprador"] and p.comprador:
            g["comprador"] = p.comprador
    return [{
        "Paquetes Despachados": i + 1,
        "Numero de Etiqueta": ", ".join(g["trk"]),
        "ID de Venta": g["venta"],
        "Nombre de la Persona": g["comprador"],
        "Cantidad de Prendas": g["cant"],
        "SKU Despachados": ", ".join(g["skus"]),
        "Estado": estado,
    } for i, g in enumerate(grupos.values())]


def _por_tipo(paquetes: list[Paquete]) -> dict[str, list[Paquete]]:
    out: dict[str, list[Paquete]] = {}
    for p in paquetes:
        t = p.lote.tipo if p.lote.tipo in ("flex", "colecta", "tiendanube") else "flex"
        out.setdefault(t, []).append(p)
    return out


def enviar_despachados(db: Session, paquetes: list[Paquete], usuario: Usuario | None = None) -> list[dict]:
    """Manda a Drive los despachados que todavía no se informaron. Uno por tipo (Flex/Colecta/TN)."""
    pend = [p for p in paquetes if p.estado == "despachado" and not p.drive_enviado]
    resultados = []
    for tipo, lista in _por_tipo(pend).items():
        r = gas_post("procesarExcel", {"datos": _items(lista, "despachado"), "fecha": hoy().isoformat()}, tipo)
        for p in lista:
            p.drive_enviado = True
        db.commit()
        registrar(db, usuario, "drive_guardado", f"{tipo} · {len(lista)} paquetes · {r.get('fileName', '')}", "despacho")
        resultados.append({"tipo": tipo, "cantidad": len(lista), "archivo": r.get("fileName", ""),
                           "url": r.get("fileUrl", "")})
    return resultados


def enviar_manual(db: Session, e: EnvioManual) -> bool:
    """Manda un envío SmartPost al Apps Script (acción envioManual, igual que el scanner)."""
    try:
        gas_post("envioManual", {"nombre": e.nombre, "articulo": e.articulo, "numero": e.numero,
                                 "plataforma": e.plataforma, "fecha": e.fecha.isoformat(), "hora": e.hora}, timeout=60)
        e.drive_ok, e.drive_error = True, ""
    except DriveError as ex:
        e.drive_ok, e.drive_error = False, str(ex)[:300]
    db.commit()
    return e.drive_ok


def guardar_config(mail: str, copia: str, hora) -> dict:
    return gas_post("setConfig", {"mail": mail, "copia": copia, "hora": hora}, timeout=60)


def informar_dia(db: Session, usuario: Usuario | None = None, fecha: date | None = None) -> dict:
    """Antes del cierre: sube los despachados que falten y anota los pendientes del día como 'pendiente'."""
    fecha = fecha or hoy()
    for e in db.scalars(select(EnvioManual).where(EnvioManual.fecha == fecha, EnvioManual.drive_ok.is_(False))).all():
        enviar_manual(db, e)
    lotes = db.scalars(select(Lote).where(or_(Lote.fecha == fecha,
                                              and_(Lote.fecha < fecha, Lote.cerrado.is_(False))))).all()
    paquetes = [p for l in lotes for p in l.paquetes]
    desp = enviar_despachados(db, paquetes, usuario)
    pendientes = [p for p in paquetes if p.estado == "pendiente" and p.drive_pend_fecha != fecha]
    pend_res = []
    for tipo, lista in _por_tipo(pendientes).items():
        gas_post("procesarExcel", {"datos": _items(lista, "pendiente"), "fecha": fecha.isoformat()}, tipo)
        for p in lista:
            p.drive_pend_fecha = fecha
        db.commit()
        registrar(db, usuario, "drive_pendientes", f"{tipo} · {len(lista)} sin despachar", "despacho")
        pend_res.append({"tipo": tipo, "cantidad": len(lista)})
    return {"despachados": desp, "pendientes": pend_res}


def cerrar_dia(db: Session, usuario: Usuario | None = None) -> dict:
    """Informa todo lo del día y le pide a Google que arme el archivo único y mande el mail."""
    info = informar_dia(db, usuario)
    r = gas_post("cerrarDia", timeout=180)
    registrar(db, usuario, "cierre_google",
              f"{r.get('despachados', 0)} despachados / {r.get('noDespachados', 0)} no despachados → {r.get('mail', 'gerencia')}")
    return {**r, "informado": info}


def leer_config() -> dict:
    try:
        return gas_post("getConfig", timeout=30)
    except DriveError:
        return {}
