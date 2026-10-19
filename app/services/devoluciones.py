"""Devoluciones: variantes del catálogo (color / talle) y envío al Apps Script "Devoluciones ML"."""
from __future__ import annotations

import json
import re
import urllib.request

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from .. import config
from ..models import Articulo, Devolucion

TALLES = re.compile(r"^(XXXS|XXS|XS|S|M|L|XL|XXL|XXXL|XXXXL|[2-6]XL|U|UNICO|ÚNICO|T\d|\d{1,2})$", re.I)
MARCAS = {"no brand", "nobrand", "parka", "parca", "manki", "puffers"}


def es_parka_o_puffers():
    """Las devoluciones son siempre de artículos Parka o Puffers (SKU ...-PARK / ...-PUFE)."""
    return or_(Articulo.sku.like("%-PARK"), Articulo.sku.like("%-PUFE"),
               Articulo.variante.ilike("%parka%"), Articulo.variante.ilike("%parca%"),
               Articulo.variante.ilike("%puffers%"))


def es_no_brand():
    return or_(Articulo.sku.like("%-NOBR"), Articulo.variante.ilike("%no brand%"),
               Articulo.variante.ilike("%nobrand%"))


def es_conteo():
    """El conteo incluye Parka, Puffers y NO BRAND."""
    return or_(es_parka_o_puffers(), es_no_brand())


def color_talle(a: Articulo) -> tuple[str, str]:
    """De "00AXE (Navystripes, XXL)" / "ABBA 00018 (XL, Parka)" saca color y talle."""
    partes = [p.strip() for p in (a.variante or "").split(",") if p.strip()]
    talle = a.talle or next((p for p in partes if TALLES.match(p)), "")
    colores = [p for p in partes if p.upper() != talle.upper() and p.lower() not in MARCAS and not TALLES.match(p)]
    color = ", ".join(colores) or a.color or "Único"
    return color, talle or "Único"


def marca(a: Articulo) -> str:
    sku = (a.sku or "").upper()
    if sku.endswith("-PUFE") or "puffers" in (a.variante or "").lower():
        return "Puffers"
    if sku.endswith("-PARK") or any(k in (a.variante or "").lower() for k in ("parka", "parca")):
        return "Parka"
    partes = [p.strip() for p in (a.variante or "").split(",") if p.strip()]
    m = next((p for p in partes if p.lower() in MARCAS), "")
    if sku.endswith("-NOBR") or m.lower() in ("no brand", "nobrand"):
        return "No Brand"
    return m.capitalize()


# Equivalencias de búsqueda: "W7", "w 7", "W-7", "w007" → W-00007 (género + número del SKU de Odoo,
# 1-CAMP-W-00007-…); "7" o "114" → 00007 / 00114 (número del modelo con ceros a 5 dígitos).
_GEN_NUM = re.compile(r"^([A-Za-z])[\s\-_./]*0*(\d{1,5})$")
_NUM = re.compile(r"^0*(\d{1,5})$")


def tokens_busqueda(q: str) -> list[str]:
    """Separa la búsqueda en palabras, juntando "W 7" / "W - 7" en un solo término."""
    q = re.sub(r"(?<![A-Za-z0-9])([A-Za-z])[\s\-_./]+(\d{1,5})(?![0-9])", r"\1\2", q.strip())
    return [t for t in q.split() if t]


def condicion_token(p: str):
    """Condición de una palabra: nombre o SKU que la contengan, más la equivalencia con el número del SKU."""
    ops = [Articulo.nombre.ilike(f"%{p}%"), Articulo.sku.ilike(f"%{p}%")]
    m = _GEN_NUM.match(p)
    if m and int(m.group(2)) > 0:
        g, n = m.group(1).upper(), int(m.group(2))
        ops.append(Articulo.sku.ilike(f"%-{g}-{n:05d}-%"))
    m = _NUM.match(p)
    if m and int(m.group(1)) > 0:
        n = int(m.group(1))
        ops += [Articulo.sku.ilike(f"%-{n:05d}-%"), Articulo.nombre.ilike(f"%{n:05d}%")]
    return or_(*ops)


def buscar_modelos(db: Session, q: str, limite: int = 30, filtro=None) -> list[dict]:
    q = q.strip()
    if not q:
        return []
    filtro = es_parka_o_puffers() if filtro is None else filtro
    # Entran todas las variantes activas de Odoo, también las que todavía no tienen código de barras
    # (se marcan "sin código": la etiqueta sale con el SKU y se listan para cargarles código en Odoo)
    base = select(Articulo).where(or_(Articulo.codigo_barras != "", Articulo.sku != ""), Articulo.inactivo.is_not(True), filtro)
    if q.isdigit() and len(q) >= 8:
        from .catalogo import por_codigo
        por_codigo(db, q)  # si el código no está local, lo trae de Odoo
    exacto = db.scalar(base.where(or_(Articulo.codigo_barras == q, Articulo.sku == q.upper())))
    if exacto:
        modelos_q = [exacto.articulo]
    else:
        cond = [condicion_token(p) for p in tokens_busqueda(q)]
        modelos_q = []
        for (m,) in db.execute(select(Articulo.articulo).where(or_(Articulo.codigo_barras != "", Articulo.sku != ""),
                                                               Articulo.inactivo.is_not(True), filtro, *cond)
                               .distinct().order_by(Articulo.articulo).limit(limite)).all():
            modelos_q.append(m)
    salida = []
    for m in modelos_q:
        vs = db.scalars(base.where(Articulo.articulo == m).order_by(Articulo.id)).all()
        variantes = []
        for a in vs:
            color, talle = color_talle(a)
            variantes.append({"id": a.id, "color": color, "talle": talle, "marca": marca(a), "sku": a.sku,
                              "codigo_barras": a.codigo_barras, "nombre": a.nombre, "sin_codigo": not a.codigo_barras})
        salida.append({"articulo": m, "variantes": variantes,
                       "preseleccion": exacto.id if exacto and exacto.articulo == m else None})
    return salida


def enviar(dev: Devolucion) -> dict:
    """Manda la devolución al Apps Script (crea Crédito y Reingreso en Drive y avisa por mail)."""
    payload = {
        "responsable": dev.usuario.nombre if dev.usuario else "PARKA Depósito",
        "sesion": f"PD-{dev.id}",
        "fecha": dev.creado_at.isoformat(timespec="seconds"),
        "plataforma": dev.plataforma or "",
        # "sector" lleva la plataforma (ML / TN) para que se vea en la columna del Excel de Drive,
        # y el nº de venta / orden, así se ve en el Excel sin tocar el Apps Script ("ML · 2000…", "TN · 1234")
        "items": [{"codigo": i.codigo_barras or i.sku, "articulo": i.articulo, "color": i.color, "talle": i.talle,
                   "estado": i.estado, "cantidad": i.cantidad,
                   "sector": " · ".join(x for x in (i.plataforma or "", i.venta or "") if x),
                   "plataforma": i.plataforma or "", "venta": i.venta or ""} for i in dev.items],
    }
    req = urllib.request.Request(config.DEVOL_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "text/plain;charset=utf-8"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    if isinstance(data, dict) and (data.get("ok") is False or ("error" in data and data.get("ok") is not True)):
        raise RuntimeError(str(data.get("error", "error del script")))
    return data


# ─────────────── Venta / orden de la devolución (Mercado Libre o Tienda Nube) ───────────────
# Cada prenda devuelta lleva el nº de venta (ML) o el nº de orden (TN). Se puede escribir o escanear la
# etiqueta del paquete (QR / código de barras de ML, nº de orden o guía de SmartPost de TN) y la app busca
# la compra en la plataforma para mostrarla y confirmar que existe.

ESTADOS_ML = {"paid": "Pagada", "confirmed": "Confirmada", "cancelled": "Cancelada", "invalid": "Inválida",
              "partially_refunded": "Reintegro parcial", "payment_required": "Pago pendiente",
              "payment_in_process": "Pago en proceso", "partially_paid": "Pago parcial"}
ESTADOS_TN = {"open": "Abierta", "closed": "Archivada", "cancelled": "Cancelada"}
ENVIO_TN = {"unpacked": "por empaquetar", "partially_packed": "por empaquetar", "in_preparation": "en preparación",
            "unshipped": "por enviar", "packed": "por enviar", "shipped": "enviada", "delivered": "entregada",
            "fulfilled": "enviada", "partially_fulfilled": "enviada en parte"}


def codigo_de_etiqueta(raw: str) -> str:
    """Lo que devuelve el lector o la cámara → el número que sirve.
    QR de ML '{"id":"47383333425","t":"lm"}' / 'LA,{…}' (también con el teclado del lector cambiado),
    prefijos AIM de Code128 (']C0', '>:'), '#1234' de Tienda Nube o el número solo."""
    s = str(raw or "").strip()
    if not s:
        return ""
    m = re.search(r"\{.*\}", s)
    if m:
        try:
            j = json.loads(m.group(0))
            if isinstance(j, dict) and j.get("id"):
                return str(j["id"]).strip()
        except ValueError:
            pass
    m = re.search(r"id[^0-9A-Za-z]{1,4}(\d{8,})", s, re.I)          # QR leído con otra distribución de teclado
    if m:
        return m.group(1)
    if re.match(r"^\]C\d", s):
        s = s[3:]
    elif re.match(r"^>[:\d]", s):
        s = s[2:]
    s = s.strip().lstrip("#").strip()
    return s.replace(" ", "") if re.fullmatch(r"[\d ]+", s) else s


def _fecha(txt) -> str:
    t = str(txt or "")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else ""


def _no(motivo: str, sin_conexion: bool = False) -> dict:
    return {"ok": False, "motivo": motivo, "sin_conexion": sin_conexion}


def _items_ml(o: dict) -> list[dict]:
    from .ventas_api import _var
    out = []
    for oi in o.get("order_items") or []:
        it = oi.get("item") or {}
        out.append({"titulo": str(it.get("title") or ""), "sku": str(it.get("seller_sku") or it.get("seller_custom_field") or ""),
                    "color": _var(it, "color", "color principal"), "talle": _var(it, "talle", "size", "talla"),
                    "cantidad": int(oi.get("quantity") or 1)})
    return out


def venta_ml(codigo: str) -> dict:
    """Nº de venta (2000…), nº de pack/carrito o nº de envío de la etiqueta → la venta de Mercado Libre."""
    from . import meli
    c = codigo_de_etiqueta(codigo)
    if not c.isdigit():
        return _no("Eso no es un número de venta ni una etiqueta de Mercado Libre")
    if not meli.configurado():
        return _no("Mercado Libre no está conectado (Conexiones → Mercado Libre directo): no se puede verificar la venta",
                   sin_conexion=True)
    try:
        ordenes, envio = [], ""
        if c.startswith("2000") and len(c) >= 13:          # nº de venta o de pack (carrito)
            try:
                ordenes = [meli.get(f"/orders/{c}") or {}]
            except meli.MeliError as e:
                if e.status not in (400, 404):
                    raise
                try:
                    pack = meli.get(f"/packs/{c}") or {}
                except meli.MeliError as e2:
                    if e2.status in (400, 404):
                        return _no(f"Mercado Libre no encontró la venta {c}")
                    raise
                ordenes = [meli.get(f"/orders/{x.get('id')}") or {} for x in pack.get("orders") or [] if x.get("id")]
        else:                                               # etiqueta: nº de envío
            try:
                sh = meli.get(f"/shipments/{c}") or {}
            except meli.MeliError as e:
                if e.status in (400, 404):
                    return _no(f"Mercado Libre no encontró la venta ni el envío {c}")
                raise
            envio = c
            oids = [sh.get("order_id")] if sh.get("order_id") else []
            if not oids:
                try:
                    its = meli.get(f"/shipments/{c}/items") or []
                    oids = list(dict.fromkeys(i.get("order_id") for i in its if isinstance(i, dict) and i.get("order_id")))
                except meli.MeliError:
                    oids = []
            if not oids:
                return _no(f"El envío {c} no tiene una venta asociada en Mercado Libre")
            ordenes = [meli.get(f"/orders/{oid}") or {} for oid in oids]
        ordenes = [o for o in ordenes if o.get("id")]
        if not ordenes:
            return _no(f"Mercado Libre no encontró la venta {c}")
        pack_id = next((str(o["pack_id"]) for o in ordenes if o.get("pack_id")), "")
        if pack_id and len(ordenes) == 1:                   # carrito: se traen todas las ventas del pack
            try:
                pack = meli.get(f"/packs/{pack_id}") or {}
                ids = [str(x.get("id")) for x in pack.get("orders") or [] if x.get("id")]
                ya = {str(o["id"]) for o in ordenes}
                ordenes += [meli.get(f"/orders/{i}") or {} for i in ids if i not in ya]
            except meli.MeliError:
                pass
    except meli.MeliError as e:
        if e.status in (401, 403):
            return _no(f"Mercado Libre no deja ver esa venta (¿es de otra cuenta?): {e}")
        return _no(f"No se pudo consultar Mercado Libre: {e}", sin_conexion=True)
    o0 = ordenes[0]
    estados = sorted({ESTADOS_ML.get(o.get("status") or "", o.get("status") or "") for o in ordenes} - {""})
    if not envio:
        envio = str((o0.get("shipping") or {}).get("id") or "")
    return {"ok": True, "plataforma": "ML", "numero": pack_id or str(o0["id"]),
            "ventas": [str(o["id"]) for o in ordenes], "envio": envio,
            "fecha": _fecha(o0.get("date_created")), "estado": " / ".join(estados),
            "cancelada": all((o.get("status") or "") == "cancelled" for o in ordenes),
            "comprador": str((o0.get("buyer") or {}).get("nickname") or ""),
            "items": [i for o in ordenes for i in _items_ml(o)]}


def _orden_tn(numero: str) -> dict | None:
    from . import tiendanube as tn
    lote = tn.get("/orders", q=numero, status="any", fields=tn.CAMPOS_ORDEN, per_page=20)
    for o in lote if isinstance(lote, list) else []:
        if str(o.get("number")) == numero:
            return o
    return None


def venta_tn(codigo: str) -> dict:
    """Nº de orden de Tienda Nube (o la guía de SmartPost de la etiqueta) → la orden."""
    from . import tiendanube as tn
    c = codigo_de_etiqueta(codigo)
    if not c.isdigit():
        return _no("Eso no es un número de orden ni una etiqueta de Tienda Nube")
    if not tn.configurado():
        return _no("Tienda Nube no está conectada (Conexiones → Tienda Nube): no se puede verificar la orden",
                   sin_conexion=True)
    guia = ""
    try:
        o = _orden_tn(c) if len(c) <= 6 else None
        if o is None and len(c) > 5:                        # guía de SmartPost → nº de orden
            from . import smartpost
            if smartpost.usar_en_despacho():
                try:
                    g = smartpost.orden_de_guia(c, [])
                except smartpost.SPError:
                    g = None
                if g and g.get("numero"):
                    guia = c
                    o = _orden_tn(str(g["numero"]))
    except tn.TNError as e:
        if e.status == 404:
            o = None
        else:
            return _no(f"No se pudo consultar Tienda Nube: {e}", sin_conexion=True)
    if o is None:
        return _no(f"Tienda Nube no encontró la orden {c}" if len(c) <= 6
                   else f"No encontré ninguna orden de Tienda Nube para la etiqueta {c}")
    p = tn.orden_a_paquete(o)
    est = ESTADOS_TN.get(o.get("status") or "", o.get("status") or "")
    env = ENVIO_TN.get((o.get("shipping_status") or "").lower(), "")
    return {"ok": True, "plataforma": "TN", "numero": str(o.get("number")), "ventas": [str(o.get("number"))],
            "envio": guia or p.get("tracking_alt") or "", "fecha": _fecha(o.get("created_at")),
            "estado": est + (f" · {env}" if env else ""), "cancelada": (o.get("status") or "") == "cancelled",
            "comprador": p.get("comprador") or "",
            "items": [{"titulo": i["producto"], "sku": i["sku"], "color": i["color"], "talle": i["talle"],
                       "cantidad": i["cantidad"]} for i in p["items"]]}


def buscar_venta(plataforma: str, codigo: str) -> dict:
    plataforma = (plataforma or "").strip().upper()
    if plataforma == "ML":
        return venta_ml(codigo)
    if plataforma == "TN":
        return venta_tn(codigo)
    return _no("Elegí el canal de venta: Mercado Libre o Tienda Nube")
