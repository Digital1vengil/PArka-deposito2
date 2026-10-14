"""SmartPost (sistema ePresis): verificar en Despacho las etiquetas de SmartPost de las órdenes de Tienda Nube.

La etiqueta de SmartPost trae el nº de guía (no el nº de orden de Tienda Nube). Con la API se consulta la guía
(`v2/seguimiento.json {nro_guia}`) y de lo que devuelve se saca la orden: el remito / referencia / pedido
(nº de orden de TN) y, si no aparece, el nombre del destinatario contra las órdenes "Por enviar" del día.

API: POST https://smartpostecommerce.epresis.com/api/<ruta> con {"api_token": ...} en el JSON.
El token se carga en Conexiones → SmartPost (se guarda en la base, nunca en el código).
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request

from . import ajustes

TIMEOUT = 12
CACHE_SEG = 600          # guía → orden ya resuelta (10 min)
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

# Campos de la guía donde puede venir el nº de orden de Tienda Nube
CLAVES_ORDEN = ("remito", "referencia", "pedido", "orden", "order", "external", "externo")
# Campos de texto libre: solo sirven si el número coincide con una orden de la lista del día
CLAVES_TEXTO = ("info_adicional", "observ", "comentario")
# Destinatario: caminos con "destinat"/"comprador", o nombre/apellido sueltos que no sean de remitente/sucursal/cliente
CLAVES_NOMBRE = ("destinat", "comprador")
NO_NOMBRE = ("remit", "sucursal", "cliente", "servicio", "operacion", "estado", "fechas")


class SPError(Exception):
    pass


def configurado(db=None) -> bool:
    return bool((ajustes.leer("sp_token", db) or "").strip())


def activo(db=None) -> bool:
    """Usar SmartPost en Despacho (Conexiones → SmartPost → 'Verificar guías en Despacho')."""
    return configurado(db) and (ajustes.leer("sp_despacho", db) or "1") != "0"


def olvidar():
    with _lock:
        _cache.clear()


def llamar(ruta: str, body: dict | None = None):
    token = (ajustes.leer("sp_token") or "").strip()
    if not token:
        raise SPError("SmartPost no está conectado (Conexiones → SmartPost → token).")
    base = (ajustes.leer("sp_url") or "https://smartpostecommerce.epresis.com/api/").strip()
    if not base.endswith("/"):
        base += "/"
    datos = json.dumps({"api_token": token, **(body or {})}).encode()
    req = urllib.request.Request(base + ruta.lstrip("/"), data=datos, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": "PARKA Deposito (digital1@magontex.com.ar)"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            txt = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")[:200]
        if e.code in (401, 403):
            raise SPError("SmartPost rechazó el token (revisalo en Conexiones).") from None
        raise SPError(f"SmartPost respondió {e.code}: {cuerpo}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SPError(f"No se pudo conectar con SmartPost ({getattr(e, 'reason', e)}).") from None
    try:
        return json.loads(txt) if txt.strip() else {}
    except ValueError:
        raise SPError("SmartPost devolvió algo que no es JSON.") from None


def probar() -> dict:
    d = llamar("v2/dummy-test.json")
    if not isinstance(d, dict) or str(d.get("user") or "").upper() != "OK":
        raise SPError("El token de SmartPost no es válido.")
    return {"cliente": d.get("cliente") or ""}


# ─────────────────────────── leer una guía ───────────────────────────

def _planos(x, camino=""):
    """Todos los valores simples de la respuesta, con su camino (guia.remito, guia.destinatario.nombre…)."""
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _planos(v, f"{camino}.{k}".lower() if camino else str(k).lower())
    elif isinstance(x, list):
        for v in x:
            yield from _planos(v, camino)
    elif x not in (None, ""):
        yield camino, str(x).strip()


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def leer_guia(codigo: str) -> dict:
    """Consulta la guía por nº de guía (y si no, por remito). Devuelve
    {encontrada, guia, remitos[], nombre, estado, crudo}."""
    codigo = str(codigo or "").strip()
    d = None
    for campo in ("nro_guia", "remito"):
        try:
            r = llamar("v2/seguimiento.json", {campo: codigo})
        except SPError as e:
            if "respondió 4" in str(e):
                continue
            raise
        g = r.get("guia") if isinstance(r, dict) else None
        if isinstance(g, dict) and g:
            d = r
            break
    if not d:
        return {"encontrada": False, "guia": codigo, "remitos": [], "textos": [], "nombre": "", "estado": "", "crudo": None}
    g = d["guia"]
    remitos, textos, partes_nombre, estado = [], [], [], ""
    for camino, valor in _planos(g):
        hoja = camino.split(".")[-1]
        if "fechas" in camino:
            continue
        if any(k in hoja for k in CLAVES_ORDEN):
            if valor not in remitos:
                remitos.append(valor)
        elif any(k in hoja for k in CLAVES_TEXTO):
            textos.append(valor)
        elif (any(k in camino for k in CLAVES_NOMBRE)
              or (hoja in ("nombre", "apellido", "razon_social") and not any(n in camino for n in NO_NOMBRE))) \
                and any(k in hoja for k in ("nombre", "apellido", "razon")) and not re.search(r"\d", valor) \
                and valor not in partes_nombre:
            partes_nombre.append(valor)
    f = g.get("fechas") if isinstance(g.get("fechas"), list) else []
    if f and isinstance(f[-1], dict):
        estado = str(f[-1].get("estado") or "")
    estado = estado or str(g.get("estado") or "")
    nro = str(g.get("nro_guia") or g.get("numero") or g.get("guia") or codigo)
    return {"encontrada": True, "guia": nro, "remitos": remitos, "textos": textos, "nombre": " ".join(partes_nombre[:3]),
            "estado": estado, "crudo": d}


def numeros_orden(remitos: list[str]) -> list[str]:
    """Del remito / referencia saca los posibles nº de orden de TN ("#7004", "TN-7004", "7004")."""
    out = []
    for r in remitos:
        for n in re.findall(r"\d{3,9}", str(r)):
            n = n.lstrip("0") or "0"
            if n not in out:
                out.append(n)
    return out


def orden_de_guia(codigo: str, por_enviar: list[dict]) -> dict:
    """Guía escaneada → orden de TN. `por_enviar` = paquetes TN "Por enviar" (ventas_api.tn_por_enviar).
    Devuelve {encontrada, guia, numero (orden TN o ''), via ('remito' | 'nombre' | ''), estado, nombre, remitos}."""
    clave = str(codigo or "").strip().upper()
    with _lock:
        c = _cache.get(clave)
        if c and time.monotonic() - c[0] < CACHE_SEG:
            return c[1]
    g = leer_guia(codigo)
    res = {k: g[k] for k in ("encontrada", "guia", "estado", "nombre", "remitos")}
    res.update(numero="", via="")
    if g["encontrada"]:
        numeros = {str(p.get("tracking")) for p in por_enviar}
        cands = numeros_orden(g["remitos"])
        en_lista = [n for n in cands + numeros_orden(g["textos"]) if n in numeros]
        if en_lista:
            res.update(numero=en_lista[0], via="remito")
        elif g["nombre"]:
            nom = _norm(g["nombre"])
            iguales = [p for p in por_enviar if nom and _norm(p.get("comprador")) == nom]
            if not iguales:   # mismo nombre y apellido en otro orden / con segundo nombre
                pal = set(nom.split())
                iguales = [p for p in por_enviar if len(pal) >= 2 and pal <= set(_norm(p.get("comprador")).split())]
            if len(iguales) == 1:
                res.update(numero=str(iguales[0]["tracking"]), via="nombre")
        if not res["numero"] and cands:
            res.update(numero=cands[0], via="remito_fuera")   # orden que no está "Por enviar": se explica
        if res["numero"] and res["via"] in ("remito", "nombre"):
            with _lock:
                _cache[clave] = (time.monotonic(), res)
    return res
