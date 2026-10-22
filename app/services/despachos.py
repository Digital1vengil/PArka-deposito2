"""Despachos de Magontex (app de Gonzalo, magontex-despachos.pages.dev): de ahí sale la hoja de ruta del chofer.

API de integración (cabecera X-API-Key, llave "Escaneo de bultos en entrega"):
  GET /api/integracion/pedidos?fecha=AAAA-MM-DD → {despachos: [{id, etiqueta, salidaConfirmadaAt, cantidad}],
                                                   pedidos: [{pedido, cliente, bultos, expreso, direccion, ordenEntrega,
                                                              ordenCarga, observaciones, estado, despacho: {...}}]}
  GET /api/integracion/pedidos/<venta>         → un pedido (S01104) con su despacho e historial.
La etiqueta del despacho es el chofer (ej. "Juani"). Por ahora es solo lectura: los bultos que escanea el chofer se
guardan en PArka-deposito2 (tabla entregas_despacho, enviado=False) hasta tener la ruta para avisarle a despachos.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import ahora, hoy
from ..models import CierreParadaDespacho, EntregaDespacho, Usuario
from . import ajustes, rotulos

DIAS_ATRAS = 14                # despachos de las últimas 2 semanas que todavía no se entregaron
CERRADOS = {"entregado", "entregada", "cancelado", "cancelada", "anulado", "anulada"}
CACHE_SEG = 30
_cache: dict[str, tuple[float, dict]] = {}


class DespachosError(Exception):
    pass


def configurado(db: Session | None = None) -> bool:
    return bool((ajustes.leer("despachos_url", db) or "").strip() and (ajustes.leer("despachos_api_key", db) or "").strip())


def _get(db: Session | None, ruta: str, params: dict | None = None) -> dict:
    base = (ajustes.leer("despachos_url", db) or "").strip().rstrip("/")
    clave = (ajustes.leer("despachos_api_key", db) or "").strip()
    if not base or not clave:
        raise DespachosError("Falta configurar Despachos (Conexiones → Despachos: dirección y llave)")
    url = f"{base}/integracion/{ruta}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    # Despachos (Cloudflare) rechaza con 403 el identificador por defecto de Python (Python-urllib): hay que mandar uno propio
    req = urllib.request.Request(url, headers={"X-API-Key": clave, "Accept": "application/json",
                                               "User-Agent": "PARKA-Deposito/1.0 (+reparto)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error") or ""
        except Exception:  # noqa: BLE001
            msg = ""
        raise DespachosError(f"Despachos respondió {e.code}{': ' + msg if msg else ''}") from e
    except (OSError, ValueError) as e:
        raise DespachosError(f"No se pudo conectar con Despachos: {e}") from e


def pedidos_del_dia(db: Session | None, fecha: date) -> dict:
    clave = fecha.isoformat()
    c = _cache.get(clave)
    if c and time.time() - c[0] < CACHE_SEG:
        return c[1]
    d = _get(db, "pedidos", {"fecha": clave})
    _cache[clave] = (time.time(), d)
    return d


def olvidar() -> None:
    _cache.clear()


def _norm(s: str) -> str:
    return rotulos._norm(s or "")


def es_del_chofer(etiqueta: str, chofer: str) -> bool:
    """La etiqueta del despacho nombra al chofer: "Juani", "Juani 2do viaje" o "Lo lleva Gonzalo" son de "Juani" /
    "Gonzalo" (o "Juani Pérez"). Se busca el primer nombre del chofer como palabra suelta dentro de la etiqueta."""
    e, c = _norm(etiqueta), _norm(chofer)
    if not e or not c:
        return False
    return e == c or c.split(" ")[0] in e.split(" ")


def despachos_recientes(db: Session | None, dias: int = DIAS_ATRAS) -> list[dict]:
    """Despachos de los últimos días con sus pedidos (el más nuevo primero)."""
    out: dict[str, dict] = {}
    for n in range(dias):
        f = hoy() - timedelta(days=n)
        d = pedidos_del_dia(db, f)
        for dsp in d.get("despachos") or []:
            out.setdefault(dsp["id"], {**dsp, "fecha": f.isoformat(), "pedidos": []})
        for p in d.get("pedidos") or []:
            dsp = p.get("despacho") or {}
            if dsp.get("id"):
                out.setdefault(dsp["id"], {**dsp, "fecha": dsp.get("fecha") or f.isoformat(), "pedidos": []})["pedidos"].append(p)
    return sorted(out.values(), key=lambda x: x.get("fecha", ""), reverse=True)


def abierto(dsp: dict) -> bool:
    return any(_norm(p.get("estado", "")) not in CERRADOS for p in dsp["pedidos"])


def terminado(db: Session, dsp: dict) -> bool:
    """El chofer ya cerró todas las paradas (Despachos no marca la entrega, así que sin esto quedaría "en viaje")."""
    paradas = {clave_parada(p) for p in dsp["pedidos"]}
    return bool(paradas) and paradas <= set(cierres(db, dsp["id"]))


def despachos_del_chofer(db: Session, chofer: Usuario) -> list[dict]:
    return [d for d in despachos_recientes(db) if es_del_chofer(d.get("etiqueta", ""), chofer.nombre) and abierto(d)
            and not terminado(db, d)]


def despacho(db: Session, chofer: Usuario, did: str) -> dict | None:
    return next((d for d in despachos_del_chofer(db, chofer) if d["id"] == did), None)


# ───────────────────────── hoja de ruta (misma forma que hojas_ruta.detalle) ─────────────────────────
def _dir(texto: str) -> tuple[str, str]:
    """"Av. Rabanal 276, Capital Federal, Ciudad Autónoma de Buenos Aires (AR)" → (calle, localidad).
    Se descartan los "0" que trae Odoo en la ciudad y los pedazos repetidos (CABA aparece como ciudad y como provincia)."""
    partes: list[str] = []
    for x in re.sub(r"\s*\([A-Z]{2}\)\s*$", "", texto or "").split(","):
        x = x.strip()
        if x and x != "0" and _norm(x) not in [_norm(y) for y in partes]:
            partes.append(x)
    return (partes[0] if partes else "", ", ".join(partes[1:]))


def clave_parada(p: dict) -> str:
    """Pedidos del mismo expreso van a la misma parada (su depósito); PROPIO o sin expreso, a la del cliente."""
    exp = p.get("expreso") or ""
    base = ("c|" + _norm(p.get("cliente", "")) + "|" + _norm(p.get("direccion", ""))) if rotulos._norm(exp) in ("", "propio") \
        else "e|" + _norm(exp)
    return hashlib.sha1(base.encode()).hexdigest()[:10]


def entregas(db: Session, did: str) -> dict[tuple[str, int], EntregaDespacho]:
    return {(e.pedido, e.bulto): e for e in db.scalars(select(EntregaDespacho).where(EntregaDespacho.despacho_id == did))}


def cierres(db: Session, did: str) -> dict[str, CierreParadaDespacho]:
    return {c.parada: c for c in db.scalars(select(CierreParadaDespacho).where(CierreParadaDespacho.despacho_id == did))}


def hoja(db: Session, dsp: dict) -> dict:
    ents, cie = entregas(db, dsp["id"]), cierres(db, dsp["id"])
    grupos: dict[str, dict] = {}
    for p in sorted(dsp["pedidos"], key=lambda x: (x.get("ordenEntrega") or 999, x.get("pedido", ""))):
        k = clave_parada(p)
        propio = rotulos._norm(p.get("expreso") or "") in ("", "propio")
        calle, loc = _dir(p.get("direccion", ""))
        g = grupos.setdefault(k, {"id": k, "orden": 0, "tipo": "cliente" if propio else "expreso",
                                  "nombre": p.get("cliente", "") if propio else p.get("expreso", ""), "direccion": calle,
                                  "localidad": loc, "telefono": "", "clientes": {}, "obs": []})
        if p.get("observaciones"):
            g["obs"].append(f"{p['pedido']}: {p['observaciones']}")
        c = g["clientes"].setdefault(_norm(p.get("cliente", "")), {"id": 0, "nombre": p.get("cliente", ""), "codigo": "",
                                                                    "pedidos": []})
        total = max(1, int(p.get("bultos") or 1))
        bs = [{"bulto": i, "entregado": (p["pedido"], i) in ents,
               "at": ents[(p["pedido"], i)].at.strftime("%H:%M") if (p["pedido"], i) in ents else ""} for i in range(1, total + 1)]
        c["pedidos"].append({"id": p["pedido"], "nombre": p["pedido"], "origen": p["pedido"], "total": total, "bultos": bs,
                             "entregados": sum(1 for b in bs if b["entregado"]), "estado": p.get("estado", "")})
    paradas = []
    for i, g in enumerate(grupos.values(), start=1):
        cls = []
        for c in g.pop("clientes").values():
            cls.append({**c, "bultos": sum(len(x["bultos"]) for x in c["pedidos"]),
                        "entregados": sum(x["entregados"] for x in c["pedidos"])})
        cerr = cie.get(g["id"])
        bul, ent = sum(c["bultos"] for c in cls), sum(c["entregados"] for c in cls)
        obs = g.pop("obs")
        paradas.append({**g, "orden": i, "clientes": cls, "bultos": bul, "entregados": ent,
                        "estado": cerr.estado if cerr else "pendiente", "nota": (cerr.nota if cerr else "") or " · ".join(obs),
                        "cerrada": cerr.at.strftime("%H:%M") if cerr else ""})
    f = dsp.get("fecha", "")
    return {"id": dsp["id"], "fecha": f"{f[8:10]}/{f[5:7]}/{f[:4]}" if len(f) >= 10 else f, "fecha_iso": f,
            "estado": "abierta", "nota": dsp.get("etiqueta", ""), "chofer": dsp.get("etiqueta", ""), "paradas": paradas,
            "bultos": sum(p["bultos"] for p in paradas), "entregados": sum(p["entregados"] for p in paradas),
            "paradas_hechas": sum(1 for p in paradas if p["estado"] != "pendiente"), "origen": "despachos"}


def resumen(db: Session, dsp: dict) -> dict:
    h = hoja(db, dsp)
    return {k: h[k] for k in ("id", "fecha", "estado", "chofer", "nota", "bultos", "entregados", "paradas_hechas")} | \
           {"paradas": len(h["paradas"])}


# ───────────────────────── escaneo ─────────────────────────
def leer_codigo(db: Session, texto: str) -> tuple[str, int, int] | None:
    """QR del rótulo → (venta, bulto, total). Acepta el código nuevo (S01104-3-9) y el viejo (id de Odoo 412-3-7)."""
    cod = rotulos.leer_codigo_bulto(texto)
    if not cod:
        return None
    ref, b, t = cod
    if isinstance(ref, str):
        return ref, b, t
    from . import odoo
    try:
        filas = odoo.cliente(db).call("stock.picking", "read", [ref], fields=["origin"])
    except Exception:  # noqa: BLE001 — sin Odoo no se puede traducir un rótulo viejo
        return None
    origen = (filas[0].get("origin") or "") if filas else ""
    return (origen, b, t) if origen else None


def escanear(db: Session, chofer: Usuario, dsp: dict, parada: str, texto: str) -> dict:
    cod = leer_codigo(db, texto)
    if not cod:
        return {"ok": False, "tipo": "invalido", "mensaje": "No es el QR de un rótulo de bulto"}
    venta, nb, total_rot = cod
    p = next((x for x in dsp["pedidos"] if _norm(x.get("pedido", "")) == _norm(venta)), None)
    if not p:
        return {"ok": False, "tipo": "no_esta", "mensaje": f"El pedido {venta} no está en este despacho"}
    total = max(1, int(p.get("bultos") or 1))
    info = {"cliente": p.get("cliente", ""), "pedido": p["pedido"], "bulto": nb, "total": total}
    if nb > total:
        return {"ok": False, "tipo": "no_esta", "bulto": info,
                "mensaje": f"El pedido {p['pedido']} tiene {total} bultos en Despachos (el rótulo dice {nb} de {total_rot})"}
    if clave_parada(p) != parada:
        otra = next((x for x in hoja(db, dsp)["paradas"] if x["id"] == clave_parada(p)), None)
        return {"ok": False, "tipo": "otra_parada", "bulto": info,
                "mensaje": f"Este bulto es de otra parada: {otra['orden']}. {otra['nombre']}" if otra else "Este bulto es de otra parada"}
    ya = db.scalar(select(EntregaDespacho).where(EntregaDespacho.despacho_id == dsp["id"], EntregaDespacho.pedido == p["pedido"],
                                                 EntregaDespacho.bulto == nb))
    if ya:
        return {"ok": False, "tipo": "ya", "bulto": info, "mensaje": f"Ya estaba entregado ({ya.at:%H:%M})"}
    db.add(EntregaDespacho(despacho_id=dsp["id"], pedido=p["pedido"], cliente=(p.get("cliente") or "")[:160], bulto=nb,
                           total=total, chofer_id=chofer.id, at=ahora()))
    db.commit()
    return {"ok": True, "tipo": "ok", "bulto": info, "mensaje": f"{p.get('cliente', '')} · bulto {nb} de {total}"}


def cerrar_parada(db: Session, chofer: Usuario, dsp: dict, parada: str, nota: str = "") -> dict:
    h = hoja(db, dsp)
    par = next((x for x in h["paradas"] if x["id"] == parada), None)
    if not par:
        raise DespachosError("Esa parada no es de este despacho")
    faltan = par["bultos"] - par["entregados"]
    c = db.scalar(select(CierreParadaDespacho).where(CierreParadaDespacho.despacho_id == dsp["id"],
                                                    CierreParadaDespacho.parada == parada))
    if not c:
        c = CierreParadaDespacho(despacho_id=dsp["id"], parada=parada)
        db.add(c)
    c.estado, c.nota, c.chofer_id, c.at = ("incompleta" if faltan else "entregada"), nota[:300], chofer.id, ahora()
    c.nombre = par["nombre"][:160]
    db.commit()
    return {"estado": c.estado, "faltan": faltan}
