"""Tienda Nube: conexión por la API oficial (app "depo" del Portal de Partners).

Cómo se conecta (una sola vez, desde Conexiones → Tienda Nube):
  1. Se cargan App ID y Client Secret de la app del Portal de Partners.
  2. "Autorizar en Tienda Nube" abre https://www.tiendanube.com/apps/{app_id}/authorize; se aceptan los permisos.
  3. Tienda Nube vuelve a la URL de redirección con ?code=…; la app lo canjea en /apps/authorize/token
     y guarda access_token + user_id (= id de la tienda). El token no vence hasta desinstalar la app.
     Si la redirección no llega a la app, el código se pega a mano en Conexiones.

Uso:
  • ordenes_pendientes(): órdenes pagadas, abiertas y sin enviar → paquetes para Recolección Ecommerce.
  • marcar_despachada(numero): al despachar, la fulfillment order pasa a DISPATCHED (= "enviada").
Las escrituras impactan en la tienda de verdad.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from . import ajustes

API = "https://api.tiendanube.com/v1"
AUTH_URL = "https://www.tiendanube.com/apps/{app_id}/authorize"
TOKEN_URL = "https://www.tiendanube.com/apps/authorize/token"
USER_AGENT = "PARKA Deposito (digital1@magontex.com.ar)"
TIMEOUT = 20
PAUSA = 0.25          # la API de TN deja ~2 pedidos/seg (leaky bucket de 40)
SCOPES = "read_orders, read_fulfillment_orders, write_fulfillment_orders"
_lock = threading.Lock()
_ultima = [0.0]


class TNError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


# ─────────────────────────── configuración ───────────────────────────

def configurado(db=None) -> bool:
    return bool((ajustes.leer("tn_access_token", db) or "").strip() and (ajustes.leer("tn_store_id", db) or "").strip())


def url_autorizar(db=None) -> str:
    app_id = (ajustes.leer("tn_app_id", db) or "").strip()
    if not app_id:
        raise TNError("Falta el App ID de Tienda Nube (Conexiones → Tienda Nube).")
    return AUTH_URL.format(app_id=urllib.parse.quote(app_id))


def canjear_codigo(db, code: str) -> dict:
    """Cambia el ?code= de la autorización por el access_token y lo guarda."""
    code = (code or "").strip()
    if "code=" in code:  # pegaron la URL entera
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query).get("code", [""])[0]
    if not code:
        raise TNError("Falta el código de autorización.")
    app_id = (ajustes.leer("tn_app_id", db) or "").strip()
    secret = (ajustes.leer("tn_client_secret", db) or "").strip()
    if not app_id or not secret:
        raise TNError("Cargá y guardá primero el App ID y el Client Secret.")
    datos = json.dumps({"client_id": app_id, "client_secret": secret,
                        "grant_type": "authorization_code", "code": code}).encode()
    req = urllib.request.Request(TOKEN_URL, data=datos, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise TNError(f"Tienda Nube rechazó el código ({e.code}): {e.read().decode('utf-8', 'replace')[:200]}", e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TNError(f"No se pudo conectar con Tienda Nube ({getattr(e, 'reason', e)}).") from None
    except ValueError:
        raise TNError("Tienda Nube devolvió algo que no es JSON.") from None
    token, tienda = j.get("access_token"), j.get("user_id")
    if not token or not tienda:
        motivo = j.get("error_description") or j.get("error") or j
        raise TNError(f"No vino el token: {motivo}. El código se usa una sola vez y vence rápido: autorizá de nuevo.")
    ajustes.guardar(db, "tn_access_token", str(token))
    ajustes.guardar(db, "tn_store_id", str(tienda))
    # Con la autorización nueva (p. ej. después de sumar permisos) las órdenes que fallaron vuelven a intentarse
    try:
        from ..models import TnEnvio
        from sqlalchemy import select
        for e in db.scalars(select(TnEnvio).where(TnEnvio.estado == "error")).all():
            e.intentos = 0
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    return {"tienda": str(tienda), "scope": j.get("scope", "")}


# ─────────────────────────── llamadas ───────────────────────────

def _cfg() -> tuple[str, str]:
    token = (ajustes.leer("tn_access_token") or "").strip()
    tienda = (ajustes.leer("tn_store_id") or "").strip()
    if not token or not tienda:
        raise TNError("Tienda Nube no está conectada (Conexiones → Tienda Nube → Autorizar).")
    return token, tienda


def _esperar_turno():
    with _lock:
        falta = PAUSA - (time.monotonic() - _ultima[0])
        if falta > 0:
            time.sleep(falta)
        _ultima[0] = time.monotonic()


def _pedir(metodo: str, path: str, body: dict | None = None, params: dict | None = None):
    token, tienda = _cfg()
    url = f"{API}/{tienda}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    h = {"Authentication": f"bearer {token}", "Authorization": f"Bearer {token}",
         "User-Agent": USER_AGENT, "Accept": "application/json"}
    datos = None
    if body is not None:
        datos = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    for intento in range(3):
        _esperar_turno()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=datos, method=metodo, headers=h),
                                        timeout=TIMEOUT) as r:
                txt = r.read().decode("utf-8", "replace")
                return json.loads(txt) if txt.strip() else {}
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("utf-8", "replace")
            if e.code == 429 and intento < 2:
                time.sleep(2)
                continue
            try:
                j = json.loads(cuerpo)
            except ValueError:
                j = {}
            motivo = (j.get("description") or j.get("message") or j.get("error") or "") if isinstance(j, dict) else ""
            if e.code == 401:
                raise TNError("El token de Tienda Nube no es válido (¿se desinstaló la app?). Autorizá de nuevo.", 401) from None
            if e.code == 403:
                raise TNError("A la app le falta un permiso en Tienda Nube" + (f": {motivo}" if motivo else "")
                              + ". Revisá los permisos en el Portal de Partners y autorizá de nuevo.", 403) from None
            if e.code == 404:
                if "Last page is 0" in cuerpo or "last page" in cuerpo.lower():
                    return []  # TN responde 404 cuando no hay resultados en el listado
                raise TNError("Tienda Nube no lo encontró (404).", 404) from None
            raise TNError(f"Tienda Nube respondió {e.code}" + (f": {motivo}" if motivo else f": {cuerpo[:200]}"), e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TNError(f"No se pudo conectar con Tienda Nube ({getattr(e, 'reason', e)}).") from None
        except ValueError:
            raise TNError("Tienda Nube devolvió algo que no es JSON.") from None
    raise TNError("Tienda Nube está limitando los pedidos (429): probá en un minuto.", 429)


def get(path: str, **params):
    return _pedir("GET", path, params=params)


def tienda() -> dict:
    d = get("/store", fields="id,name,url_with_protocol,original_domain")
    return d if isinstance(d, dict) else {}


def probar() -> dict:
    t = tienda()
    nombre = t.get("name")
    if isinstance(nombre, dict):
        nombre = nombre.get("es") or next(iter(nombre.values()), "")
    n = len(ordenes_pendientes(max_paginas=1))
    return {"ok": True, "tienda": nombre or "?", "id": t.get("id"), "pendientes": n}


# ─────────────────────────── órdenes ───────────────────────────

CAMPOS_ORDEN = ("id,number,status,payment_status,shipping_status,created_at,contact_name,customer,"
                "shipping_address,products,shipping_option,shipping_tracking_number,shipping_pickup_type")
ENVIADA = {"shipped", "delivered", "fulfilled", "partially_fulfilled"}


def _txt(x) -> str:
    if isinstance(x, dict):
        return str(x.get("es") or next(iter(x.values()), "") or "")
    return str(x or "")


POR_EMPAQUETAR = {"unpacked", "partially_packed", "in_preparation"}


def es_pendiente(o: dict) -> bool:
    """Lo que el admin de TN muestra en "Por empaquetar": pagada, abierta y sin empaquetar
    (también parcialmente empaquetadas / en preparación). Las ya empaquetadas ("Por enviar") no entran."""
    if (o.get("status") or "") != "open":
        return False
    if (o.get("payment_status") or "") not in ("paid", "authorized"):
        return False
    return (o.get("shipping_status") or "").lower() in POR_EMPAQUETAR


POR_ENVIAR = {"unshipped", "packed"}


def es_por_enviar(o: dict) -> bool:
    """Lo que el admin de TN muestra en "Por enviar": pagada, abierta y ya empaquetada pero sin enviar.
    Es lo que se despacha (Despacho en vivo)."""
    if (o.get("status") or "") != "open":
        return False
    if (o.get("payment_status") or "") not in ("paid", "authorized"):
        return False
    return (o.get("shipping_status") or "").lower() in POR_ENVIAR


def orden_a_paquete(o: dict) -> dict:
    """Orden de la API → el mismo paquete que arma el CSV de TN (parsers.construir_tiendanube)."""
    from ..parsers import variante_tn
    numero = str(o.get("number") or o.get("id"))
    cliente = o.get("customer") or {}
    dir_ = o.get("shipping_address") or {}
    comprador = (dir_.get("name") if isinstance(dir_, dict) else "") or o.get("contact_name") \
        or (cliente.get("name") if isinstance(cliente, dict) else "") or ""
    items, productos, skus, total = [], [], [], 0
    for p in o.get("products") or []:
        nombre, sku = _txt(p.get("name")), str(p.get("sku") or "")
        try:
            cant = int(float(p.get("quantity") or 1))
        except (TypeError, ValueError):
            cant = 1
        color, talle = variante_tn(nombre)
        vv = [_txt(v) for v in (p.get("variant_values") or []) if _txt(v)]
        if not (color or talle) and vv:
            color, talle = (", ".join(vv[:-1]), vv[-1]) if len(vv) >= 2 else (vv[0], "")
        items.append(dict(sku=sku, producto=nombre, color=color, talle=talle, cantidad=max(1, cant),
                          variante=" · ".join(x for x in (f"Color: {color}" if color else "",
                                                          f"Talle: {talle}" if talle else "") if x)))
        total += max(1, cant)
        if sku and sku not in skus:
            skus.append(sku)
        if nombre and nombre not in productos:
            productos.append(nombre)
    envio = o.get("shipping_option")
    return dict(tracking=numero, venta_id=numero, comprador=str(comprador)[:120], producto=" + ".join(productos),
                sku=", ".join(skus), cantidad=total or 1, tracking_alt=str(o.get("shipping_tracking_number") or ""),
                items=items, estado_envio=str(o.get("shipping_status") or ""), estado_orden=str(o.get("status") or ""),
                medio=_txt(envio.get("name") if isinstance(envio, dict) else envio), tipo="tiendanube",
                tn_id=o.get("id"))


def ordenes_pendientes(dias: int = 30, max_paginas: int = 10) -> list[dict]:
    """Órdenes "Por empaquetar" de los últimos `dias` días, como paquetes (orden más vieja primero)."""
    return _ordenes_abiertas(es_pendiente, dias, max_paginas)


def ordenes_por_enviar(dias: int = 30, max_paginas: int = 10) -> list[dict]:
    """Órdenes "Por enviar" (empaquetadas, sin enviar) de los últimos `dias` días: lo que sale en Despacho."""
    return _ordenes_abiertas(es_por_enviar, dias, max_paginas)


def _ordenes_abiertas(filtro, dias: int, max_paginas: int) -> list[dict]:
    from ..db import ahora
    desde = (ahora() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00-03:00")
    out: list[dict] = []
    for pagina in range(1, max_paginas + 1):
        lote = get("/orders", status="open", payment_status="paid", created_at_min=desde,
                   per_page=200, page=pagina, fields=CAMPOS_ORDEN)
        if not isinstance(lote, list) or not lote:
            break
        out += [orden_a_paquete(o) for o in lote if filtro(o)]
        if len(lote) < 200:
            break
    out.sort(key=lambda p: int(p["tracking"]) if p["tracking"].isdigit() else 0)
    return out


def buscar_orden(numero: str) -> dict | None:
    """Por nº de orden (el que se escanea en Despacho) → la orden con su id interno."""
    numero = str(numero).strip().lstrip("#")
    lote = get("/orders", q=numero, fields="id,number,status,shipping_status", per_page=20)
    for o in lote if isinstance(lote, list) else []:
        if str(o.get("number")) == numero:
            return o
    return None


def marcar_despachada(numero: str) -> dict:
    """Pasa las fulfillment orders de la orden a DISPATCHED (la orden queda como enviada)."""
    o = buscar_orden(numero)
    if not o:
        raise TNError(f"No encontré la orden #{numero} en Tienda Nube.", 404)
    oid = o["id"]
    fos = get(f"/orders/{oid}/fulfillment-orders")
    fos = fos if isinstance(fos, list) else (fos.get("fulfillment_orders") or fos.get("results") or []) if isinstance(fos, dict) else []
    hechas, ya = 0, 0
    for fo in fos:
        st = str(fo.get("status") or "").upper()
        if st in ("DISPATCHED", "DELIVERED", "READY_FOR_PICKUP"):
            ya += 1
            continue
        _pedir("PATCH", f"/orders/{oid}/fulfillment-orders/{fo['id']}", body={"status": "DISPATCHED"})
        hechas += 1
    if not fos:
        raise TNError(f"La orden #{numero} no tiene órdenes de envío (fulfillment orders) para marcar.")
    return {"orden_id": oid, "marcadas": hechas, "ya_estaban": ya}


# ─────────────────────────── cola de "enviadas" ───────────────────────────
MAX_INTENTOS = 5


def marcar_activo(db=None) -> bool:
    return (ajustes.leer("tn_marcar_despacho", db) or "1").strip() not in ("0", "", "no", "false")


def procesar_envio(db, numero: str, usuario_id: int | None = None) -> dict:
    """Registra la salida de una orden y la marca enviada en TN. Si falla queda para reintentar."""
    from ..db import ahora
    from ..models import TnEnvio
    from sqlalchemy import select
    numero = str(numero or "").strip().lstrip("#")
    if not numero.isdigit():
        return {"ok": False, "estado": "ignorado", "mensaje": "No es un nº de orden de Tienda Nube"}
    e = db.scalar(select(TnEnvio).where(TnEnvio.numero == numero))
    if e and e.estado == "ok":
        return {"ok": True, "estado": "ok", "mensaje": f"La orden #{numero} ya estaba marcada como enviada"}
    if not e:
        e = TnEnvio(numero=numero, usuario_id=usuario_id, intentos=0, estado="pendiente", error="")
        db.add(e)
    e.intentos += 1
    try:
        r = marcar_despachada(numero)
        e.estado, e.error, e.orden_id, e.hecho_at = "ok", "", str(r["orden_id"]), ahora()
        msg = f"Orden #{numero} marcada como enviada en Tienda Nube"
    except TNError as ex:
        e.estado, e.error = "error", str(ex)[:500]
        if ex.status in (404,) or "no tiene órdenes de envío" in str(ex):
            e.intentos = max(e.intentos, MAX_INTENTOS)  # no tiene sentido reintentar
        elif ex.status == 403:
            e.intentos = min(e.intentos, MAX_INTENTOS - 1)  # falta un permiso: se reintenta cuando se autorice de nuevo
        msg = str(ex)
    db.commit()
    return {"ok": e.estado == "ok", "estado": e.estado, "mensaje": msg}


def reintentar(db) -> int:
    """Para el scheduler: reintenta las que fallaron (máx. MAX_INTENTOS)."""
    from ..models import TnEnvio
    from sqlalchemy import select
    if not configurado(db):
        return 0
    n = 0
    for e in db.scalars(select(TnEnvio).where(TnEnvio.estado == "error", TnEnvio.intentos < MAX_INTENTOS)
                        .order_by(TnEnvio.id).limit(20)).all():
        if procesar_envio(db, e.numero)["ok"]:
            n += 1
    return n


def ultimos_envios(db, limite: int = 15) -> list[dict]:
    from ..models import TnEnvio
    from sqlalchemy import select
    return [{"numero": e.numero, "estado": e.estado, "error": e.error, "intentos": e.intentos,
             "hora": (e.hecho_at or e.creado_at).strftime("%d/%m %H:%M")}
            for e in db.scalars(select(TnEnvio).order_by(TnEnvio.id.desc()).limit(limite)).all()]
