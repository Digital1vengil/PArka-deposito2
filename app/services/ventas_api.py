"""Ventas para preparar, directo de las APIs (sin bajar archivos):

• Tienda Nube: las órdenes "Por empaquetar" (services/tiendanube.py).
• Mercado Libre (Flex y Colecta, por el proxy de la app de Costos): los envíos listos para salir, separados en
    - "por_imprimir": la etiqueta todavía no se imprimió (ready_to_ship / ready_to_print) → "Etiquetas por imprimir"
    - "lista":        la etiqueta ya está impresa (ready_to_ship / printed …)          → "Listas para despachar"
  Los que ya salieron, cancelados, Full u otra logística no entran.
  `hoy` = ML pide despacharlo hoy o antes (fecha límite de despacho); los de próximos días vienen aparte.

Cada venta se devuelve como el mismo "paquete" que arman los archivos (tracking, items…), con `clave` =
"tn:<nº orden>" o "ml:<nº envío>". Se cachea unos minutos: ML cobra un pedido por envío.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from ..db import ahora, hoy
from . import meli
from . import tiendanube as tn

LOGISTICA = {"self_service": "flex", "cross_docking": "colecta", "xd_drop_off": "colecta"}
POR_IMPRIMIR = {"ready_to_print", "invoice_pending"}
YA_SALIO = {"picked_up", "dropped_off", "in_transit", "in_hub", "in_warehouse", "on_route_to_pickup",
            "picking_up", "rejected_in_hub", "claimed_me", "stale"}
CACHE_SEG = 110
ETIQUETAS = {"por_imprimir": "Etiqueta por imprimir", "lista": "Lista para despachar"}

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None}
_finales: set[str] = set()   # envíos que ya no van a volver a estar pendientes (salieron, cancelados, otra logística)
_estado: dict[str, str] = {}  # envío ML → último status visto (cancelled, shipped…)


# ─────────────────────────── Mercado Libre ───────────────────────────

def _var(i: dict, *nombres: str) -> str:
    for a in i.get("variation_attributes") or []:
        if (a.get("name") or "").strip().lower() in nombres or (a.get("id") or "").lower() in nombres:
            return str(a.get("value_name") or "")
    return ""


def _limite(sh: dict) -> datetime | None:
    so = sh.get("shipping_option") or {}
    for d in ((so.get("estimated_handling_limit") or {}).get("date"),
              (sh.get("lead_time") or {}).get("estimated_handling_limit", {}).get("date") if isinstance(sh.get("lead_time"), dict) else None):
        if d:
            try:
                return datetime.fromisoformat(str(d).replace("Z", "+00:00"))
            except ValueError:
                pass
    return None


def _dia_despacho(sh: dict, tipo: str | None):
    """Día (de Buenos Aires) en que ML pide despachar el envío. Usa la fecha límite de despacho pasada a hora
    argentina (ML la manda en -04:00 / UTC); si no viene y es Flex, la fecha de entrega (Flex se entrega el mismo
    día que sale). None si ML no da ninguna."""
    lim = _limite(sh)
    if lim is not None:
        return _fecha_ba(lim.isoformat())
    if tipo == "flex":
        so = sh.get("shipping_option") or {}
        for k in ("estimated_delivery_time", "estimated_delivery_limit", "estimated_delivery_final"):
            d = _fecha_ba((so.get(k) or {}).get("date"))
            if d:
                return d
    return None


def ml_pendientes(dias: int = 10, max_ordenes: int = 600) -> list[dict]:
    if not meli.configurado():
        raise meli.MeliError("Mercado Libre no está conectado (Conexiones → Mercado Libre directo).")
    seller = meli.seller_id()
    desde = (ahora() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000-03:00")
    por_envio: dict[str, list[dict]] = {}
    offset = 0
    while offset < max_ordenes:
        d = meli.get(f"/orders/search?seller={seller}&order.status=paid&order.date_created.from={desde}"
                     f"&sort=date_desc&limit=50&offset={offset}") or {}
        res = d.get("results") or []
        for o in res:
            if "delivered" in (o.get("tags") or []):
                continue
            sid = str((o.get("shipping") or {}).get("id") or "")
            if sid and sid not in _finales:
                por_envio.setdefault(sid, []).append(o)
        total = (d.get("paging") or {}).get("total") or 0
        offset += 50
        if len(res) < 50 or offset >= total:
            break
    out = []
    for sid, ordenes in por_envio.items():
        try:
            sh = meli.get(f"/shipments/{sid}") or {}
        except meli.MeliError as e:
            if e.status == 404:
                _finales.add(sid)
                continue
            raise
        st, sub, lg = sh.get("status"), (sh.get("substatus") or ""), sh.get("logistic_type")
        _estado[sid] = st or ""
        tipo = LOGISTICA.get(lg or "")
        if st in ("cancelled", "shipped", "delivered", "not_delivered") or (lg and not tipo) or sub in YA_SALIO:
            _finales.add(sid)
            continue
        if st != "ready_to_ship" or not tipo:
            continue
        etiqueta = "por_imprimir" if sub in POR_IMPRIMIR else "lista"
        lim = _limite(sh)
        dia = _dia_despacho(sh, tipo)
        items, total = [], 0
        for o in ordenes:
            for it in o.get("order_items") or []:
                i = it.get("item") or {}
                cant = int(it.get("quantity") or 1)
                color, talle = _var(i, "color", "color principal"), _var(i, "talle", "size", "tamaño")
                items.append(dict(sku=str(i.get("seller_sku") or ""), producto=str(i.get("title") or ""),
                                  color=color, talle=talle, cantidad=cant,
                                  variante=" · ".join(f"{a.get('name')}: {a.get('value_name')}"
                                                      for a in i.get("variation_attributes") or [])))
                total += cant
        o0 = ordenes[0]
        rec = (sh.get("receiver_address") or {}).get("receiver_name") or ""
        out.append(dict(
            clave=f"ml:{sid}", tracking=sid, venta_id=str(next((o.get("pack_id") for o in ordenes if o.get("pack_id")), None) or o0.get("id") or ""),
            comprador=(rec or (o0.get("buyer") or {}).get("nickname") or "")[:120], tipo=tipo,
            producto=" + ".join(i["producto"] for i in items), sku=", ".join(i["sku"] for i in items if i["sku"]),
            cantidad=total or 1, items=items, etiqueta=etiqueta, subestado=sub,
            ordenes=[str(o.get("id")) for o in ordenes if o.get("id")],
            hasta=lim.strftime("%d/%m %H:%M") if lim else "", hoy=(dia is None or dia <= hoy()),
            atrasada=bool(dia and dia < hoy()), dia=dia.isoformat() if dia else ""))
    out.sort(key=lambda p: (not p["hoy"], p["tipo"], p["etiqueta"] != "por_imprimir", p["tracking"]))
    return out


def _items_ml(ordenes: list[dict]) -> list[dict]:
    items = []
    for o in ordenes:
        for it in o.get("order_items") or []:
            i = it.get("item") or {}
            items.append(dict(sku=str(i.get("seller_sku") or ""), producto=str(i.get("title") or ""),
                              color=_var(i, "color", "color principal"), talle=_var(i, "talle", "size", "tamaño"),
                              cantidad=int(it.get("quantity") or 1), variante=""))
    return items


def _fecha_ba(txt) -> "date | None":
    """Fecha (día de Buenos Aires) de un texto ISO de ML / TN."""
    if not txt:
        return None
    try:
        d = datetime.fromisoformat(str(txt).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is not None:
        from ..config import TZ
        d = d.astimezone(TZ)
    return d.date()


def ml_canceladas(dias: int = 0) -> list[dict]:
    """Lo mismo que el panel de ML "Canceladas. No despachar": ventas de Flex / Colecta canceladas HOY que ya
    tenían la etiqueta impresa y salían hoy. Las canceladas de días anteriores (aunque ML les haya tocado la
    fecha de actualización) y las que se cancelaron antes de imprimir la etiqueta no entran."""
    seller = meli.seller_id()
    desde = (ahora() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000-03:00")
    por_envio: dict[str, list[dict]] = {}
    for offset in range(0, 200, 50):
        d = meli.get(f"/orders/search?seller={seller}&order.status=cancelled&order.date_last_updated.from={desde}"
                     f"&sort=date_desc&limit=50&offset={offset}") or {}
        res = d.get("results") or []
        for o in res:
            sid = str((o.get("shipping") or {}).get("id") or "")
            if sid:
                por_envio.setdefault(sid, []).append(o)
        if len(res) < 50 or offset + 50 >= int((d.get("paging") or {}).get("total") or 0):
            break
    out = []
    dia = hoy()
    for sid, ordenes in por_envio.items():
        info = _canceladas_info.get(sid)
        if info is None:
            try:
                sh = meli.get(f"/shipments/{sid}") or {}
            except meli.MeliError:
                continue
            hist = sh.get("status_history")
            lista = not isinstance(hist, dict) or bool(hist.get("date_ready_to_ship"))
            subs = sh.get("substatus_history")
            if "date_first_printed" in sh or subs:
                impresa = bool(sh.get("date_first_printed")) or any(
                    (x or {}).get("substatus") == "printed" for x in subs or [])
            else:
                impresa = lista
            canc = _fecha_ba((hist or {}).get("date_cancelled") if isinstance(hist, dict) else None)
            if canc is None:
                o0 = ordenes[0]
                canc = _fecha_ba((o0.get("cancel_detail") or {}).get("date") or o0.get("date_closed")
                                 or o0.get("last_updated") or o0.get("date_last_updated"))
            lim = _limite(sh)
            info = {"tipo": LOGISTICA.get(sh.get("logistic_type") or ""), "lista": lista and impresa,
                    "cancelada_el": canc.isoformat() if canc else "", "limite": lim.date().isoformat() if lim else "",
                    "comprador": (sh.get("receiver_address") or {}).get("receiver_name") or ""}
            _canceladas_info[sid] = info
            _estado[sid] = "cancelled"
            _finales.add(sid)
        if not info["tipo"] or not info["lista"]:
            continue
        if info.get("cancelada_el") and info["cancelada_el"] != dia.isoformat():
            continue                       # se canceló otro día: no es de hoy
        if info.get("limite") and info["limite"] > dia.isoformat():
            continue                       # salía otro día
        items = _items_ml(ordenes)
        o0 = ordenes[0]
        out.append(dict(clave=f"ml:{sid}", tracking=sid, tipo=info["tipo"], cancelada=True, items=items,
                        ordenes=[str(o.get("id")) for o in ordenes if o.get("id")],
                        producto=" + ".join(i["producto"] for i in items),
                        venta_id=str(o0.get("pack_id") or o0.get("id") or ""), cantidad=sum(i["cantidad"] for i in items),
                        comprador=(info["comprador"] or (o0.get("buyer") or {}).get("nickname") or "")[:120], hoy=True))
    return out


_canceladas_info: dict[str, dict] = {}


def tn_canceladas(dias: int = 0) -> list[dict]:
    """Órdenes de Tienda Nube canceladas hoy."""
    from ..db import ahora as _ah
    desde = (_ah() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00-03:00")
    lote = tn.get("/orders", status="cancelled", updated_at_min=desde, per_page=50,
                  fields=tn.CAMPOS_ORDEN + ",cancelled_at")
    dia = hoy() - timedelta(days=dias)
    return [dict(tn.orden_a_paquete(o), clave=f"tn:{o.get('number')}", cancelada=True, hoy=True)
            for o in (lote if isinstance(lote, list) else [])
            if not o.get("cancelled_at") or (_fecha_ba(o.get("cancelled_at")) or dia) >= dia]


def estado_ml(sid: str) -> str:
    """Status de un envío de ML (con memoria): para saber si una venta de la búsqueda se canceló."""
    sid = str(sid)
    if sid not in _estado:
        try:
            _estado[sid] = (meli.get(f"/shipments/{sid}") or {}).get("status") or ""
        except meli.MeliError as e:
            if e.status != 404:
                raise
            _estado[sid] = "not_found"
    return _estado[sid]


def estado_tn(numero: str) -> str:
    o = tn.buscar_orden(numero)
    return (o or {}).get("status") or ""


# ─────────────────────────── todo junto ───────────────────────────

def _tn() -> tuple[list[dict], list[dict]]:
    pend = [dict(p, clave=f"tn:{p['tracking']}", etiqueta="", hoy=True) for p in tn.ordenes_pendientes()]
    try:
        canc = tn_canceladas()
    except tn.TNError:
        canc = []
    return pend, canc


def _ml() -> tuple[list[dict], list[dict]]:
    pend = ml_pendientes()
    try:
        canc = ml_canceladas()
    except meli.MeliError:
        canc = []
    return pend, canc


def todas(refrescar: bool = False) -> dict:
    """{'tn': {'ok', 'error', 'paquetes'}, 'ml': {...}, 'hora'} — cacheado CACHE_SEG segundos."""
    with _lock:
        if not refrescar and _cache["data"] and time.monotonic() - _cache["ts"] < CACHE_SEG:
            return _cache["data"]

    def correr(fn, conectado):
        if not conectado:
            return {"ok": False, "conectado": False, "error": "", "paquetes": [], "canceladas": []}
        try:
            pend, canc = fn()
            return {"ok": True, "conectado": True, "error": "", "paquetes": pend, "canceladas": canc}
        except (tn.TNError, meli.MeliError) as e:
            return {"ok": False, "conectado": True, "error": str(e), "paquetes": [], "canceladas": []}

    with ThreadPoolExecutor(2) as ex:
        f_tn = ex.submit(correr, _tn, tn.configurado())
        f_ml = ex.submit(correr, _ml, meli.configurado())
        data = {"tn": f_tn.result(), "ml": f_ml.result(), "hora": ahora().strftime("%H:%M")}
    with _lock:
        _cache.update(ts=time.monotonic(), data=data)
    return data


def por_claves(claves: list[str]) -> list[dict]:
    d = todas()
    idx = {p["clave"]: p for k in ("tn", "ml") for p in d[k]["paquetes"]}
    faltan = [c for c in claves if c not in idx]
    if faltan:  # la caché venció o cambió: se vuelve a pedir una vez
        d = todas(refrescar=True)
        idx = {p["clave"]: p for k in ("tn", "ml") for p in d[k]["paquetes"]}
    return [idx[c] for c in claves if c in idx]


def olvidar():
    with _lock:
        _cache.update(ts=0.0, data=None)


# ─────────────────────────── Despacho en vivo ───────────────────────────
# Despacho ya no baja archivos de Drive: la lista del día sale de acá.
#   Flex / Colecta → envíos de ML "Listas para despachar" (etiqueta impresa) que ML pide despachar hoy.
#   Tienda Nube    → órdenes "Por enviar" (empaquetadas, sin enviar).
# Las canceladas se consultan aparte y más seguido (una sola búsqueda en ML) para avisar en el momento.

CANC_SEG = 45
_tn_env: dict = {"ts": 0.0, "data": None}
_canc: dict = {"ml": {"ts": 0.0, "data": None}, "tn": {"ts": 0.0, "data": None}}


def tn_por_enviar(refrescar: bool = False) -> list[dict]:
    with _lock:
        if not refrescar and _tn_env["data"] is not None and time.monotonic() - _tn_env["ts"] < CACHE_SEG:
            return _tn_env["data"]
    data = [dict(p, clave=f"tn:{p['tracking']}", hoy=True) for p in tn.ordenes_por_enviar()]
    with _lock:
        _tn_env.update(ts=time.monotonic(), data=data)
    return data


def canceladas(fuente: str, refrescar: bool = False) -> list[dict]:
    """Canceladas recientes ('ml' o 'tn'), cacheadas CANC_SEG segundos."""
    c = _canc[fuente]
    with _lock:
        if not refrescar and c["data"] is not None and time.monotonic() - c["ts"] < CANC_SEG:
            return c["data"]
    data = ml_canceladas() if fuente == "ml" else tn_canceladas()
    with _lock:
        c.update(ts=time.monotonic(), data=data)
    return data


def _para_scanner(p: dict) -> dict:
    """Paquete de la API → el formato del scanner de Despacho (state.packages)."""
    tn_ = p.get("tipo") == "tiendanube"
    d = dict(trackingId=str(p.get("tracking") or ""), ventaId=str(p.get("venta_id") or ""),
             comprador=p.get("comprador") or "", producto=p.get("producto") or "", sku=p.get("sku") or "",
             cantidad=p.get("cantidad") or 1, ordenes=p.get("ordenes") or [], hasta=p.get("hasta") or "",
             items=[{"sku": i.get("sku") or "", "producto": i.get("producto") or "", "color": i.get("color") or "",
                     "talle": i.get("talle") or "", "cantidad": i.get("cantidad") or 1} for i in p.get("items") or []],
             vivo=True, estado="pendiente", scannedAt=None, scannedCode=None)
    if p.get("tipo") == "cambio":
        d.update(medioEnvio=p.get("medio") or "", cambio=True)
    if tn_:
        d.update(orderNum=int(d["trackingId"]) if d["trackingId"].isdigit() else 0,
                 tnTracking=p.get("tracking_alt") or "", medioEnvio=p.get("medio") or "")
    return d


def despacho(tipo: str, refrescar: bool = False) -> dict:
    """Lista del día para el scanner. Lanza MeliError / TNError si la conexión falla."""
    from ..db import hoy as _hoy
    base = {"tipo": tipo, "fecha": _hoy().isoformat(), "hora": ahora().strftime("%H:%M")}
    if tipo == "tiendanube":
        if not tn.configurado():
            raise tn.TNError("Tienda Nube no está conectada (Conexiones → Tienda Nube).")
        paq = tn_por_enviar(refrescar)
        try:
            canc = canceladas("tn", refrescar)
        except tn.TNError:
            canc = []
        return dict(base, paquetes=[_para_scanner(p) for p in paq], por_imprimir=0, proximos=0,
                    canceladas=[_para_scanner(p) for p in canc])
    if tipo == "cambios":
        from . import cambios as cam
        if not cam.configurado():
            raise cam.CambiosError("La app de cambios no está conectada (Conexiones → App de cambios).")
        paq, canc = cam.para_busqueda(refrescar=refrescar)
        return dict(base, paquetes=[_para_scanner(p) for p in paq], por_imprimir=0, proximos=0,
                    canceladas=[_para_scanner(p) for p in canc])
    if tipo not in ("flex", "colecta"):
        raise ValueError("tipo inválido")
    if not meli.configurado():
        raise meli.MeliError("Mercado Libre no está conectado (Conexiones → Mercado Libre directo).")
    d = todas(refrescar)["ml"]
    if not d["ok"]:
        raise meli.MeliError(d["error"] or "No se pudo leer Mercado Libre.")
    mios = [p for p in d["paquetes"] if p["tipo"] == tipo]
    listas = [p for p in mios if p["etiqueta"] == "lista" and p["hoy"]]
    try:
        canc = [p for p in canceladas("ml", refrescar) if p["tipo"] == tipo]
    except meli.MeliError:
        canc = []
    return dict(base, paquetes=[_para_scanner(p) for p in listas],
                por_imprimir=sum(1 for p in mios if p["etiqueta"] == "por_imprimir" and p["hoy"]),
                proximos=sum(1 for p in mios if not p["hoy"]),
                canceladas=[_para_scanner(p) for p in canc])


_ESTADOS_ML = {"cancelled": "VENTA CANCELADA — NO DESPACHAR", "shipped": "Ya fue despachado (está en camino)",
               "delivered": "Ya fue entregado", "not_delivered": "Envío no entregado / devuelto",
               "pending": "El envío todavía no está listo en ML", "handling": "El envío todavía no está listo en ML"}


def explicar_envio(codigo: str, tipo: str) -> dict:
    """Para una etiqueta escaneada que no está en la lista del día: qué pasa con ella en ML / TN.
    Devuelve {motivo, mensaje, paquete?}; motivo 'agregar' = es una lista para despachar de hoy de este tipo
    (entró después de cargar la lista) y viene `paquete` para sumarla."""
    codigo = str(codigo or "").strip().lstrip("#")
    if tipo == "cambios":
        from . import cambios as cam
        s_ = cam.buscar(codigo, refrescar=True)
        if not s_:
            return {"motivo": "no_existe", "mensaje": "No es un cambio (escaneá o escribí CAM-000N o el nº de orden)"}
        est = str(s_.get("Estado") or "")
        if est in cam.ESTADOS_CERRADOS:
            return {"motivo": "cancelada", "mensaje": f"CAMBIO {est.upper()} — NO DESPACHAR"}
        if est in cam.ESTADOS_SALIDOS:
            return {"motivo": "ya_salio", "mensaje": f"El cambio ya figura {est}"}
        if est not in cam.estados_preparar():
            return {"motivo": "no_lista", "mensaje": f"El cambio está \"{est}\": todavía no se despacha"}
        return {"motivo": "agregar", "mensaje": "Cambio para despachar (se suma a la lista)",
                "paquete": _para_scanner(cam.a_paquete(s_))}
    if tipo == "tiendanube":
        o = tn.buscar_orden(codigo) if codigo.isdigit() else None
        if not o:
            return {"motivo": "no_existe", "mensaje": "No es una orden de Tienda Nube"}
        st, env = (o.get("status") or ""), (o.get("shipping_status") or "").lower()
        if st == "cancelled":
            return {"motivo": "cancelada", "mensaje": "ORDEN CANCELADA — NO DESPACHAR"}
        if env in POR_EMPAQUETAR_TN:
            return {"motivo": "no_lista", "mensaje": "La orden todavía está \"Por empaquetar\" en Tienda Nube"}
        if env in tn.ENVIADA:
            return {"motivo": "ya_salio", "mensaje": "La orden ya figura enviada en Tienda Nube"}
        for p in tn_por_enviar(refrescar=True):
            if p["tracking"] == str(o.get("number")):
                return {"motivo": "agregar", "mensaje": "Orden por enviar (se suma a la lista)", "paquete": _para_scanner(p)}
        return {"motivo": "otro", "mensaje": f"Estado en Tienda Nube: {st} / {env}"}
    if not codigo.isdigit():
        return {"motivo": "no_existe", "mensaje": "No es una etiqueta de Mercado Libre"}
    sid = codigo
    if codigo.startswith("2000") and len(codigo) >= 15:   # nº de venta / pack → su envío
        try:
            o = meli.get(f"/orders/{codigo}") or {}
            sid = str((o.get("shipping") or {}).get("id") or "")
        except meli.MeliError:
            sid = ""
        if not sid:
            try:
                o = meli.get(f"/packs/{codigo}") or {}
                sid = str((o.get("shipment") or {}).get("id") or "")
            except meli.MeliError:
                sid = ""
        if not sid:
            return {"motivo": "no_existe", "mensaje": "ML no encontró esa venta"}
    try:
        sh = meli.get(f"/shipments/{sid}") or {}
    except meli.MeliError as e:
        if e.status == 404:
            return {"motivo": "no_existe", "mensaje": "ML no reconoce este envío (¿es de otra cuenta o del correo?)"}
        raise
    st, sub, lg = sh.get("status") or "", sh.get("substatus") or "", sh.get("logistic_type") or ""
    _estado[sid] = st
    otro = LOGISTICA.get(lg)
    if st == "cancelled":
        return {"motivo": "cancelada", "mensaje": _ESTADOS_ML["cancelled"], "envio": sid}
    if st != "ready_to_ship" or sub in YA_SALIO:
        return {"motivo": "ya_salio" if st in ("shipped", "delivered") or sub in YA_SALIO else "no_lista",
                "mensaje": _ESTADOS_ML.get(st, f"Estado en ML: {st} {sub}".strip()), "envio": sid}
    if not otro:
        return {"motivo": "otra_logistica", "mensaje": "Este envío no es Flex ni Colecta (otra logística)", "envio": sid}
    if otro != tipo:
        return {"motivo": "otro_tipo", "mensaje": f"Este envío es {otro.upper()}, no {tipo.upper()}", "envio": sid}
    dia = _dia_despacho(sh, otro)
    if dia and dia > hoy():
        return {"motivo": "otro_dia", "mensaje": f"Es para despachar el {dia.strftime('%d/%m')}, no hoy", "envio": sid}
    if sub in POR_IMPRIMIR:
        return {"motivo": "no_lista", "mensaje": "En ML figura con la etiqueta por imprimir", "envio": sid}
    for p in todas(refrescar=True)["ml"]["paquetes"]:
        if p["tracking"] == sid and p["tipo"] == tipo and p["etiqueta"] == "lista":
            return {"motivo": "agregar", "mensaje": "Lista para despachar (se suma a la lista)",
                    "paquete": _para_scanner(p), "envio": sid}
    return {"motivo": "otro", "mensaje": "ML lo da listo, pero no se pudo traer la venta", "envio": sid}


POR_EMPAQUETAR_TN = tn.POR_EMPAQUETAR
