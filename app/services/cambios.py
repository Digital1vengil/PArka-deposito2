"""App de cambios (Panel de Cambios · PARKA, Cloudflare Worker) ↔ PARKA Depósito.

El panel expone un único endpoint POST {base}/api con {"action": ...}. Se entra con la clave del panel
(action "login" → cookie de sesión) y después:
  • adminListar                         → {"solicitudes": [...], "estados": [...]}
  • adminActualizar {id, cambios: {...}} → cambia campos (ej. {"Estado": "Despachado"})

Qué hace Depósito con los cambios:
  • Los que están en los estados "a preparar" (Conexiones → App de cambios; por defecto Aceptado, Etiqueta enviada
    y Recibido) entran a la búsqueda del día de Recolección Ecommerce como "Cambio CAM-000N": se busca la prenda que
    le mandamos al cliente ("Enviar al cliente") y se controla en la mesa.
  • Rechazado / Cancelado mientras está en la búsqueda → sale de la búsqueda con aviso (como una venta cancelada).
  • Sección Cambios: la lista de lo que hay que despachar y "Despachado" (escaneando CAM-000N o el nº de orden),
    que en la app de cambios pasa el estado a Despachado.
La clave del panel se carga a mano en Conexiones (nunca en el código).
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

from . import ajustes

TIMEOUT = 20
CACHE_SEG = 60
ESTADOS_PREPARAR = "Aceptado,Etiqueta enviada,Recibido"
ESTADOS_CERRADOS = {"Rechazado", "Cancelado"}
ESTADOS_SALIDOS = {"Despachado", "Finalizado"}
_lock = threading.Lock()
_sesion = {"cookie": "", "base": ""}
_cache = {"ts": 0.0, "data": None}


class CambiosError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


# ─────────────────────────── configuración ───────────────────────────

def configurado(db=None) -> bool:
    return bool((ajustes.leer("cambios_clave", db) or "").strip())


def _base(db=None) -> str:
    return (ajustes.leer("cambios_url", db) or "").strip().rstrip("/").removesuffix("/admin").removesuffix("/api")


def estados_preparar(db=None) -> list[str]:
    return [e.strip() for e in (ajustes.leer("cambios_estados", db) or ESTADOS_PREPARAR).split(",") if e.strip()]


def marcar_activo(db=None) -> bool:
    return (ajustes.leer("cambios_marcar_despacho", db) or "1").strip() not in ("0", "", "no", "false")


def olvidar():
    with _lock:
        _sesion.update(cookie="", base="")
        _cache.update(ts=0.0, data=None)


# ─────────────────────────── llamadas ───────────────────────────

def _post(base: str, cuerpo: dict, cookie: str = "") -> tuple[int, dict, list[str]]:
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "PARKA-Deposito/1.0"}
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(f"{base}/api", data=json.dumps(cuerpo).encode(), method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            st, txt, cookies = r.status, r.read().decode("utf-8", "replace"), r.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        st, txt, cookies = e.code, e.read().decode("utf-8", "replace"), e.headers.get_all("Set-Cookie") or []
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CambiosError(f"No se pudo conectar con la app de cambios ({getattr(e, 'reason', e)}).") from None
    try:
        j = json.loads(txt) if txt.strip() else {}
    except ValueError:
        raise CambiosError(f"La app de cambios respondió algo que no es JSON ({st}).", st) from None
    return st, j if isinstance(j, dict) else {"data": j}, cookies


def _login(base: str) -> str:
    clave = (ajustes.leer("cambios_clave") or "").strip()
    if not clave:
        raise CambiosError("Falta la clave del panel de cambios (Conexiones → App de cambios).")
    st, j, cookies = _post(base, {"action": "login", "password": clave})
    if st >= 400 or j.get("ok") is False:
        raise CambiosError("La clave del panel de cambios no es válida." if st in (401, 403) or j.get("ok") is False
                           else f"La app de cambios respondió {st} al entrar.", st)
    cookie = "; ".join(c.split(";", 1)[0] for c in cookies if "=" in c.split(";", 1)[0])
    if not cookie:
        raise CambiosError("La app de cambios no devolvió la sesión (cookie) al entrar.")
    return cookie


def api(action: str, **data) -> dict:
    base = _base()
    if not base:
        raise CambiosError("Falta la dirección de la app de cambios (Conexiones → App de cambios).")
    with _lock:
        if not _sesion["cookie"] or _sesion["base"] != base:
            _sesion.update(cookie=_login(base), base=base)
        cookie = _sesion["cookie"]
    st, j, _ = _post(base, {"action": action, **data}, cookie)
    if st == 401:   # sesión vencida: se entra de nuevo una vez
        with _lock:
            _sesion.update(cookie=_login(base), base=base)
            cookie = _sesion["cookie"]
        st, j, _ = _post(base, {"action": action, **data}, cookie)
    if st >= 400 or j.get("ok") is False:
        raise CambiosError(f"App de cambios ({action}): {j.get('error') or st}", st)
    return j


# ─────────────────────────── solicitudes → paquetes ───────────────────────────

_ITEM = re.compile(r"^\s*(?:(\d+)\s*[x×]\s*)?(.*?)\s*(?:\(SKU\s*([^)]+)\))?\s*$", re.I)


def items_de(texto: str) -> list[dict]:
    """"1x Campera Vesubio — Verde / S (SKU M-112-GREEN-S)" → [{cantidad 1, producto, color Verde, talle S, sku}]."""
    out = []
    for parte in re.split(r"\n|;|\s\|\s|\s\+\s", texto or ""):
        if not parte.strip():
            continue
        m = _ITEM.match(parte)
        cant, prod, sku = (m.group(1), m.group(2), m.group(3)) if m else ("1", parte, "")
        color = talle = ""
        if "—" in prod or " - " in prod:
            nombre, _, var = prod.partition("—") if "—" in prod else prod.partition(" - ")
            prod = nombre.strip()
            partes = [x.strip() for x in var.split("/") if x.strip()]
            if partes:
                color, talle = (" / ".join(partes[:-1]), partes[-1]) if len(partes) > 1 else (partes[0], "")
        out.append(dict(sku=(sku or "").strip(), producto=prod.strip(), color=color, talle=talle,
                        cantidad=int(cant or 1),
                        variante=" · ".join(x for x in (f"Color: {color}" if color else "",
                                                        f"Talle: {talle}" if talle else "") if x)))
    return out


def _orden(s: dict) -> str:
    m = re.search(r"\d+", str(s.get("Orden") or ""))
    return m.group(0) if m else ""


def a_paquete(s: dict) -> dict:
    cid = str(s.get("ID") or "").strip()
    items = items_de(str(s.get("Enviar al cliente") or ""))
    return dict(clave=f"cam:{cid}", tracking=cid, venta_id=_orden(s), comprador=str(s.get("Cliente") or "")[:120],
                tipo="cambio", items=items, cantidad=sum(i["cantidad"] for i in items) or 1,
                producto=" + ".join(i["producto"] for i in items), sku=", ".join(i["sku"] for i in items if i["sku"]),
                medio=str(s.get("Envío") or ""), estado_cambio=str(s.get("Estado") or ""), hoy=True, etiqueta="")


def solicitudes(refrescar: bool = False) -> list[dict]:
    with _lock:
        if not refrescar and _cache["data"] is not None and time.monotonic() - _cache["ts"] < CACHE_SEG:
            return _cache["data"]
    j = api("adminListar")
    data = j.get("solicitudes") or j.get("data") or []
    with _lock:
        _cache.update(ts=time.monotonic(), data=data)
    return data


def para_busqueda(refrescar: bool = True) -> tuple[list[dict], list[dict]]:
    """(cambios a preparar, cambios rechazados / cancelados) como paquetes."""
    prep = set(estados_preparar())
    pend, canc = [], []
    for s in solicitudes(refrescar):
        est = str(s.get("Estado") or "")
        if not s.get("ID") or not str(s.get("Enviar al cliente") or "").strip():
            continue
        if est in prep:
            pend.append(a_paquete(s))
        elif est in ESTADOS_CERRADOS:
            canc.append(a_paquete(s))
    return pend, canc


def buscar(codigo: str, refrescar: bool = False) -> dict | None:
    """Por ID (CAM-0002, 0002, 2) o por nº de orden (1277 / #1277)."""
    c = re.sub(r"[^A-Z0-9]", "", str(codigo or "").upper())
    if not c:
        return None
    for s in solicitudes(refrescar):
        sid = re.sub(r"[^A-Z0-9]", "", str(s.get("ID") or "").upper())
        if c == sid or (c.isdigit() and sid.endswith(c.zfill(4)) and len(c) <= 4) or (c.isdigit() and c == _orden(s)):
            return s
    return None


def marcar(cid: str, estado: str = "Despachado") -> dict:
    j = api("adminActualizar", id=cid, cambios={"Estado": estado})
    with _lock:
        for s in _cache["data"] or []:
            if s.get("ID") == cid:
                s["Estado"] = estado
    return j


def probar() -> dict:
    olvidar()
    ss = solicitudes(refrescar=True)
    prep = set(estados_preparar())
    return {"total": len(ss), "a_preparar": sum(1 for s in ss if s.get("Estado") in prep)}
