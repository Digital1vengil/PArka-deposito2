"""Rótulos de entrega y códigos de clientes (Odoo, solo lectura).

Pedidos: operaciones de Magontex (almacén WH) "Recolectar" (WH/PICK) y "Órdenes de entrega" (WH/OUT). La app anota
cuándo vio cada pedido por primera vez (tabla odoo_rotulos) y si ya se imprimió su rótulo.
De cada pedido salen: bultos (campo "Cantidad de Bultos" de Odoo, se puede corregir al imprimir), cliente (contacto
principal: nombre, CUIT, teléfono, Transportista por defecto = expreso) y dirección (la de entrega del pedido).

Código de cliente: CLI- + id de Odoo del contacto principal con 6 dígitos (CLI-011661). Fijo y único, no se guarda.
El QR lleva a la ficha del cliente en la app (/clientes/<id>).

Rótulo: Zebra 10 x 15 cm a 300 dpi. ZPL nativo o HTML de 100 x 150 mm para imprimir con el driver.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from html import escape, unescape

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import ahora
from ..models import OdooRotulo, Usuario
from . import ajustes, odoo
from .etiquetas_rollo import code128

TIPOS = {"PICK": "Recolectar", "OUT": "Órdenes de entrega"}
ESTADOS = {"draft": "Borrador", "waiting": "Esperando otra operación", "confirmed": "En espera", "assigned": "Listo"}
CERRADOS = {"done": "Hecho", "cancel": "Cancelado"}
DOMINIO_BASE = [["picking_type_id.warehouse_id.code", "=", "WH"], ["picking_type_id.sequence_code", "in", list(TIPOS)]]
DOMINIO = DOMINIO_BASE + [["state", "in", list(ESTADOS)]]
LIMITE_TODOS = 400
CAMPOS_PICK = ["name", "origin", "partner_id", "state", "scheduled_date", "create_date", "date_done", "picking_type_id",
               "qty_bultos", "carrier_id", "weight", "observations", "carrier_tracking_ref"]
CAMPOS_PARTNER = ["name", "type", "parent_id", "commercial_partner_id", "street", "street2", "city", "zip", "state_id",
                  "phone", "mobile", "email", "vat", "default_transportista", "is_company"]
DOMINIO_CLIENTES = [["customer_rank", ">", 0], ["parent_id", "=", False]]
_CACHE: dict = {"t": 0.0, "data": None}
_CACHE_TODOS: dict = {"t": 0.0, "data": None}
_CACHE_CLIENTES: dict = {"t": 0.0, "data": None}
_CACHE_EMPRESA: dict = {"t": 0.0, "data": None}
CACHE_SEG = 45
CACHE_CLIENTES_SEG = 600


def codigo_cliente(pid: int) -> str:
    return f"CLI-{int(pid):06d}"


def id_de_codigo(texto: str) -> int | None:
    """"CLI-011661", "cli011661" o "11661" → 11661 (lo que lee el scanner o se escribe en el buscador)."""
    m = re.fullmatch(r"\s*(?:cli-?)?(\d{1,9})\s*", texto or "", re.I)
    return int(m.group(1)) if m else None


def _m2o(v) -> str:
    return v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else ""


def _m2o_id(v) -> int:
    return v[0] if isinstance(v, (list, tuple)) and v else 0


def _t(v) -> str:
    s = (v or "").strip() if isinstance(v, str) else ""
    return "" if s in ("0", "-", ".") else s


def _sin_html(v) -> str:
    """Observaciones de Odoo (HTML) → texto de una línea."""
    if not isinstance(v, str):
        return ""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", v))).strip()


def _provincia(v) -> str:
    return re.sub(r"\s*\([A-Z]{2}\)$", "", _m2o(v))


def _fecha(v) -> str:
    """Odoo guarda en UTC → hora de Buenos Aires (UTC-3), "aaaa-mm-dd hh:mm"."""
    if not isinstance(v, str) or not v:
        return ""
    try:
        return (datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S") - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return v[:16]


# ───────────────────────── clientes ─────────────────────────
def _direccion(p: dict) -> dict:
    return {"calle": " ".join(x for x in (_t(p.get("street")), _t(p.get("street2"))) if x),
            "localidad": _t(p.get("city")), "cp": _t(p.get("zip")), "provincia": _provincia(p.get("state_id"))}


def _tel(*ps: dict) -> str:
    vistos = []
    for p in ps:
        for k in ("phone", "mobile"):
            v = _t(p.get(k))
            if v and v not in vistos:
                vistos.append(v)
    return " / ".join(vistos[:2])


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


ES_CABA = {"ciudad autonoma de buenos aires", "caba", "capital federal"}

# CP (de 4 dígitos) de las localidades más comunes, para cuando Odoo no lo tiene. (localidad, provincia) normalizadas;
# provincia "" = sirve para cualquiera. CABA no está: el CP depende de la calle.
CP_LOCALIDAD = {
    ("rosario", "santa fe"): "2000", ("santa fe", "santa fe"): "3000", ("rafaela", "santa fe"): "2300",
    ("venado tuerto", "santa fe"): "2600", ("reconquista", "santa fe"): "3560", ("villa gobernador galvez", "santa fe"): "2124",
    ("cordoba", "cordoba"): "5000", ("capital", "cordoba"): "5000", ("rio cuarto", "cordoba"): "5800",
    ("villa maria", "cordoba"): "5900", ("san francisco", "cordoba"): "2400", ("villa carlos paz", "cordoba"): "5152",
    ("la plata", "buenos aires"): "1900", ("mar del plata", "buenos aires"): "7600", ("bahia blanca", "buenos aires"): "8000",
    ("tandil", "buenos aires"): "7000", ("olavarria", "buenos aires"): "7400", ("necochea", "buenos aires"): "7630",
    ("pergamino", "buenos aires"): "2700", ("junin", "buenos aires"): "6000", ("san nicolas", "buenos aires"): "2900",
    ("zarate", "buenos aires"): "2800", ("campana", "buenos aires"): "2804", ("lujan", "buenos aires"): "6700",
    ("santa teresita", "buenos aires"): "7107", ("pinamar", "buenos aires"): "7167", ("villa gesell", "buenos aires"): "7165",
    ("olivos", "buenos aires"): "1636", ("san isidro", "buenos aires"): "1642", ("san fernando", "buenos aires"): "1646",
    ("tigre", "buenos aires"): "1648", ("vicente lopez", "buenos aires"): "1638", ("martinez", "buenos aires"): "1640",
    ("ramos mejia", "buenos aires"): "1704", ("moron", "buenos aires"): "1708", ("haedo", "buenos aires"): "1706",
    ("castelar", "buenos aires"): "1712", ("ituzaingo", "buenos aires"): "1714", ("merlo", "buenos aires"): "1722",
    ("moreno", "buenos aires"): "1744", ("san justo", "buenos aires"): "1754", ("lomas de zamora", "buenos aires"): "1832",
    ("banfield", "buenos aires"): "1828", ("lanus", "buenos aires"): "1824", ("avellaneda", "buenos aires"): "1870",
    ("quilmes", "buenos aires"): "1878", ("berazategui", "buenos aires"): "1884", ("adrogue", "buenos aires"): "1846",
    ("pilar", "buenos aires"): "1629", ("escobar", "buenos aires"): "1625", ("san miguel", "buenos aires"): "1663",
    ("mendoza", "mendoza"): "5500", ("san rafael", "mendoza"): "5600", ("godoy cruz", "mendoza"): "5501",
    ("san miguel de tucuman", "tucuman"): "4000", ("tucuman", "tucuman"): "4000", ("concepcion", "tucuman"): "4146",
    ("salta", "salta"): "4400", ("san salvador de jujuy", "jujuy"): "4600", ("jujuy", "jujuy"): "4600",
    ("santiago del estero", "santiago del estero"): "4200", ("sgo del estero cap", "santiago del estero"): "4200",
    ("la banda", "santiago del estero"): "4300", ("san fernando del valle de catamarca", "catamarca"): "4700",
    ("catamarca", "catamarca"): "4700", ("la rioja", "la rioja"): "5300", ("san juan", "san juan"): "5400",
    ("san luis", "san luis"): "5700", ("villa mercedes", "san luis"): "5730", ("neuquen", "neuquen"): "8300",
    ("cipolletti", "rio negro"): "8324", ("general roca", "rio negro"): "8332", ("bariloche", "rio negro"): "8400",
    ("san carlos de bariloche", "rio negro"): "8400", ("viedma", "rio negro"): "8500", ("santa rosa", "la pampa"): "6300",
    ("parana", "entre rios"): "3100", ("concordia", "entre rios"): "3200", ("gualeguaychu", "entre rios"): "2820",
    ("la paz", "entre rios"): "3190", ("corrientes", "corrientes"): "3400", ("resistencia", "chaco"): "3500",
    ("posadas", "misiones"): "3300", ("obera", "misiones"): "3360", ("formosa", "formosa"): "3600",
    ("comodoro rivadavia", "chubut"): "9000", ("trelew", "chubut"): "9100", ("puerto madryn", "chubut"): "9120",
    ("rio gallegos", "santa cruz"): "9400", ("ushuaia", "tierra del fuego"): "9410", ("rio grande", "tierra del fuego"): "9420",
}


def cp_de_localidad(localidad: str, provincia: str) -> str:
    loc, prov = _norm(localidad), _norm(provincia)
    if not loc or loc in ES_CABA or prov in ES_CABA:
        return ""
    prov = prov.replace("provincia de ", "")
    return CP_LOCALIDAD.get((loc, prov), "")


def completar_direccion(d_ent: dict, d_com: dict) -> dict:
    """La dirección de entrega de Odoo suele venir sin CP ni ciudad, pero el contacto principal los tiene:
    se completa con los del principal si es la misma calle, o la misma ciudad (salvo CABA, que tiene varios CP)."""
    if not d_ent["calle"]:
        dire = dict(d_com)
    else:
        dire = dict(d_ent)
        misma_calle = _norm(d_ent["calle"]) == _norm(d_com["calle"])
        if not dire["provincia"]:
            dire["provincia"] = d_com["provincia"]
        if not dire["localidad"] and (misma_calle or dire["provincia"] == d_com["provincia"]):
            dire["localidad"] = d_com["localidad"]
        loc_e, loc_c = _norm(dire["localidad"]), _norm(d_com["localidad"])
        misma_ciudad = loc_e and loc_e == loc_c and loc_e not in ES_CABA and _norm(dire["provincia"]) not in ES_CABA
        if not dire["cp"] and (misma_calle or misma_ciudad):
            dire["cp"] = d_com["cp"]
    if not dire["localidad"] and _norm(dire["provincia"]) in ES_CABA:
        dire["localidad"] = "CABA"
    if not dire["cp"]:
        dire["cp"] = cp_de_localidad(dire["localidad"], dire["provincia"])
    return dire


def armar_cliente(com: dict, ent: dict | None = None, contactos: list[dict] | None = None) -> dict:
    """Contacto principal (com) + dirección de entrega (ent, opcional) → datos del rótulo.
    contactos: personas del cliente en Odoo, por si el teléfono está cargado ahí."""
    ent = ent or {}
    dire = completar_direccion(_direccion(ent), _direccion(com))
    trans = com.get("default_transportista") or ent.get("default_transportista")
    expreso, expreso_id = _m2o(trans), _m2o_id(trans)
    return {"id": com["id"], "codigo": codigo_cliente(com["id"]), "nombre": _t(com.get("name")),
            "cuit": _t(com.get("vat")), "telefono": _tel(ent, com, *(contactos or [])), "email": _t(com.get("email")),
            "expreso": expreso, "expreso_id": expreso_id, "empresa": bool(com.get("is_company")), **dire}


def telefonos_contactos(c: "odoo.Cliente", com_ids: list[int]) -> dict[int, list[dict]]:
    """Personas de contacto (con teléfono) de cada cliente: se usan si el cliente no tiene teléfono propio."""
    if not com_ids:
        return {}
    filas = c.call("res.partner", "search_read", [["parent_id", "in", sorted(set(com_ids))], ["type", "!=", "delivery"],
                                                  "|", ["phone", "!=", False], ["mobile", "!=", False]],
                   fields=["parent_id", "phone", "mobile"], order="id")
    out: dict[int, list[dict]] = {}
    for f in filas:
        out.setdefault(_m2o_id(f.get("parent_id")), []).append(f)
    return out


def leer_partners(c: "odoo.Cliente", ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    filas = c.call("res.partner", "read", sorted(set(ids)), fields=CAMPOS_PARTNER)
    return {p["id"]: p for p in filas}


def listar_clientes(db: Session, refrescar: bool = False) -> list[dict]:
    """Clientes (contactos principales con ventas) ordenados por nombre. Caché de 10 minutos."""
    if refrescar or _CACHE_CLIENTES["data"] is None or time.time() - _CACHE_CLIENTES["t"] > CACHE_CLIENTES_SEG:
        c = odoo.cliente(db)
        filas = c.call("res.partner", "search_read", DOMINIO_CLIENTES, fields=CAMPOS_PARTNER, order="name")
        _CACHE_CLIENTES.update(t=time.time(), data=[armar_cliente(p) for p in filas])
    return _CACHE_CLIENTES["data"]


def ficha_cliente(db: Session, pid: int) -> dict | None:
    """Un cliente con sus direcciones de entrega y sus últimos pedidos de Magontex."""
    c = odoo.cliente(db)
    ps = leer_partners(c, [pid])
    if pid not in ps:
        return None
    com_id = _m2o_id(ps[pid].get("commercial_partner_id")) or pid
    if com_id != pid:
        ps.update(leer_partners(c, [com_id]))
    com = ps[com_id]
    hijos = c.call("res.partner", "search_read", [["parent_id", "=", com_id], ["type", "=", "delivery"]],
                   fields=CAMPOS_PARTNER, order="id")
    sin_tel = not _tel(com)
    cli = armar_cliente(com, contactos=telefonos_contactos(c, [com_id]).get(com_id) if sin_tel else None)
    cli["entregas"] = [{"id": h["id"], **completar_direccion(_direccion(h), _direccion(com))}
                       for h in hijos if _direccion(h)["calle"]]
    picks = c.call("stock.picking", "search_read", DOMINIO_BASE + [["partner_id", "child_of", com_id]],
                   fields=CAMPOS_PICK, order="create_date desc", limit=15)
    cli["pedidos"] = [{"id": p["id"], "nombre": p["name"], "origen": p.get("origin") or "",
                       "estado_txt": ESTADOS.get(p["state"]) or CERRADOS.get(p["state"], p["state"]),
                       "creado": _fecha(p.get("create_date")), "bultos": int(p.get("qty_bultos") or 0)} for p in picks]
    return cli


# ───────────────────────── pedidos ─────────────────────────
def direcciones_expresos(c: "odoo.Cliente", ids: list[int]) -> dict[int, dict]:
    """Dirección del depósito de cada expreso (Transportistas de Odoo: calle, ciudad, CP)."""
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    try:
        filas = c.call("delivery.carrier", "read", ids, fields=["street", "street2", "city", "zip", "state_id"])
    except odoo.OdooError:
        return {}
    return {f["id"]: _direccion(f) for f in filas}


def leer_pedidos(c: "odoo.Cliente", dominio: list | None = None, limite: int = 300) -> list[dict]:
    picks = c.call("stock.picking", "search_read", DOMINIO if dominio is None else dominio, fields=CAMPOS_PICK,
                   order="create_date desc", limit=limite)
    if not picks:
        return []
    ents = leer_partners(c, [_m2o_id(p.get("partner_id")) for p in picks if p.get("partner_id")])
    faltan = [_m2o_id(e.get("commercial_partner_id")) for e in ents.values()]
    ps = {**ents, **leer_partners(c, [i for i in faltan if i and i not in ents])}
    sin_tel = [i for i in faltan if i in ps and not _tel(ps[i])]
    contactos = telefonos_contactos(c, sin_tel)
    out = []
    for p in picks:
        ent = ps.get(_m2o_id(p.get("partner_id")))
        com = ps.get(_m2o_id(ent.get("commercial_partner_id"))) if ent else None
        cli = armar_cliente(com or ent, ent, contactos.get((com or ent or {}).get("id"))) if ent else None
        if cli and not cli["expreso"]:
            cli["expreso"], cli["expreso_id"] = _m2o(p.get("carrier_id")), _m2o_id(p.get("carrier_id"))
        tipo = _m2o(p.get("picking_type_id")).split(": ", 1)[-1]
        out.append({"id": p["id"], "nombre": p["name"], "origen": p.get("origin") or "", "tipo": tipo,
                    "estado": p["state"], "estado_txt": ESTADOS.get(p["state"]) or CERRADOS.get(p["state"], p["state"]),
                    "abierto": p["state"] in ESTADOS, "creado": _fecha(p.get("create_date")),
                    "bultos": max(1, int(p.get("qty_bultos") or 0)), "cliente": cli,
                    "peso": float(p.get("weight") or 0), "obs": _sin_html(p.get("observations")),
                    "tracking": _t(p.get("carrier_tracking_ref"))})
    dirs = direcciones_expresos(c, [o["cliente"]["expreso_id"] for o in out if o["cliente"] and o["cliente"].get("expreso_id")])
    for o in out:
        if o["cliente"]:
            o["cliente"]["expreso_dir"] = dirs.get(o["cliente"].get("expreso_id"), {})
    return out


def detectar(db: Session, pedidos: list[dict]) -> dict[int, OdooRotulo]:
    """Anota los pedidos que la app ve por primera vez (fecha de llegada). Devuelve el registro de cada uno."""
    ids = [p["id"] for p in pedidos]
    if not ids:
        return {}
    filas = {r.picking_id: r for r in db.scalars(select(OdooRotulo).where(OdooRotulo.picking_id.in_(ids))).all()}
    nuevos = [p for p in pedidos if p["id"] not in filas and p["abierto"]]
    for p in nuevos:
        r = OdooRotulo(picking_id=p["id"], nombre=p["nombre"][:40], detectado_at=ahora())
        db.add(r)
        filas[p["id"]] = r
    if nuevos:
        db.commit()
    return filas


def _estado_rotulo(r: OdooRotulo | None, nombres: dict[int, str]) -> dict:
    if not r:
        return {"detectado": "", "impreso": None}
    imp = None
    if r.impreso_at:
        imp = {"usuario": nombres.get(r.impreso_por or 0, ""), "at": r.impreso_at.strftime("%d/%m %H:%M"),
               "bultos": r.bultos, "veces": r.veces}
    return {"detectado": r.detectado_at.strftime("%Y-%m-%d %H:%M"), "impreso": imp}


def listar(db: Session, refrescar: bool = False, todos: bool = False) -> list[dict]:
    cache = _CACHE_TODOS if todos else _CACHE
    if refrescar or cache["data"] is None or time.time() - cache["t"] > CACHE_SEG:
        c = odoo.cliente(db)
        cache.update(t=time.time(), data=leer_pedidos(c, DOMINIO_BASE, LIMITE_TODOS) if todos else leer_pedidos(c))
    peds = cache["data"]
    regs = detectar(db, peds)
    nombres = dict(db.execute(select(Usuario.id, Usuario.nombre)).all())
    return [{**p, **_estado_rotulo(regs.get(p["id"]), nombres)} for p in peds]


def pedido(db: Session, pid: int) -> dict | None:
    peds = leer_pedidos(odoo.cliente(db), [["id", "=", pid]] + DOMINIO_BASE)
    if not peds:
        return None
    p = peds[0]
    regs = detectar(db, [{**p, "abierto": True}])
    return {**p, **_estado_rotulo(regs.get(pid), {})}


def registrar_impresion(db: Session, pid: int, nombre: str, usuario_id: int | None, bultos: int) -> None:
    r = db.scalar(select(OdooRotulo).where(OdooRotulo.picking_id == pid))
    if not r:
        r = OdooRotulo(picking_id=pid, nombre=nombre[:40], detectado_at=ahora())
        db.add(r)
    r.impreso_at, r.impreso_por, r.bultos, r.veces = ahora(), usuario_id, bultos, (r.veces or 0) + 1
    db.commit()


def revisar_nuevos(db: Session) -> int:
    """Tarea programada: lee los pedidos abiertos y anota la fecha de los nuevos aunque nadie abra la pantalla."""
    antes = len(_CACHE["data"] or [])
    peds = listar(db, refrescar=True)
    return max(0, len(peds) - antes)


# ───────────────────────── remitente ─────────────────────────
def remitente(db: Session | None = None) -> dict:
    """Datos de la compañía en Odoo (se leen una vez por hora) + teléfono cargado en Conexiones."""
    if _CACHE_EMPRESA["data"] is None or time.time() - _CACHE_EMPRESA["t"] > 3600:
        try:
            filas = odoo.cliente(db).call("res.company", "search_read", [], fields=[
                "name", "street", "street2", "city", "zip", "state_id", "vat"], limit=1)
        except odoo.OdooError:
            filas = []
        e = filas[0] if filas else {}
        _CACHE_EMPRESA.update(t=time.time(), data={
            "nombre": _t(e.get("name")) or "MAGONTEX S.A.", "cuit": _t(e.get("vat")), **_direccion(e)})
    return {**_CACHE_EMPRESA["data"], "telefono": (ajustes.leer("rotulo_remitente_tel", db) or "").strip()}


# ───────────────────────── rótulos ─────────────────────────
def codigo_bulto(venta: str, bulto: int, total: int) -> str:
    """Código de un bulto en el QR del rótulo: <venta>-<bulto>-<total> (ej. S01104-3-9), igual que en Despachos."""
    return f"{venta}-{int(bulto)}-{int(total)}"


def leer_codigo_bulto(texto: str) -> tuple[str | int, int, int] | None:
    """Lo que lee la cámara del QR → (venta, bulto, total). "https://…/b/S01104-3-9" o "S01104-3-9" → ("S01104", 3, 9).
    Los rótulos viejos traían el id del pedido de Odoo ("…/b/412-3-7") → (412, 3, 7)."""
    m = re.search(r"(?:^|/b/)([A-Za-z]{1,4}\d{1,9}|\d{1,9})-(\d{1,2})-(\d{1,2})\s*$", (texto or "").strip())
    if not m:
        return None
    ref, b, t = m.group(1), int(m.group(2)), int(m.group(3))
    if not 1 <= b <= t:
        return None
    return (int(ref) if ref.isdigit() else ref.upper(), b, t)


def lista_rotulos(ped: dict | None, cli: dict, rem: dict, bultos: int, url_ficha: str, fecha: str = "",
                  url_base: str = "") -> list[dict]:
    """Un rótulo por bulto ("1/3", "2/3"…). Con pedido y url_base, el QR de cada rótulo identifica ese bulto
    (url_base/b/<pedido>-<bulto>-<total>); escaneado con la cámara común abre la ficha del cliente."""
    bultos = max(1, min(99, int(bultos or 1)))
    base = {"rem": rem, "cli": cli, "url": url_ficha, "fecha": fecha or ahora().strftime("%d/%m/%Y"),
            "pedido": (ped or {}).get("origen", ""), "picking": (ped or {}).get("nombre", ""),
            "tipo": (ped or {}).get("tipo", ""), "total": bultos, "peso": (ped or {}).get("peso", 0),
            "obs": (ped or {}).get("obs", ""), "tracking": (ped or {}).get("tracking", "")}
    venta = (ped or {}).get("origen") or ""
    if venta and url_base:
        return [{**base, "bulto": i, "url": f"{url_base}/b/{codigo_bulto(venta, i, bultos)}"} for i in range(1, bultos + 1)]
    return [{**base, "bulto": i} for i in range(1, bultos + 1)]


def fecha_de(ped: dict) -> str:
    """Fecha del rótulo: el día que la app vio el pedido (si no, el de creación en Odoo)."""
    f = ped.get("detectado") or ped.get("creado") or ""
    return f"{f[8:10]}/{f[5:7]}/{f[:4]}" if len(f) >= 10 else ""


# ZPL a 300 dpi: 11,81 puntos por mm. Etiqueta Zebra de 100 x 150 mm = 1181 x 1772 puntos.
# El rótulo va pegado abajo: los mm de arriba (Conexiones → "Margen libre arriba", 40 por defecto) quedan en blanco
# porque en la etiqueta de Mercado Libre esa parte se separa.
DPMM = 300 / 25.4
PW, LL = round(100 * DPMM), round(150 * DPMM)
MX = 35   # margen lateral (puntos)
ARRIBA_MM = 40
DISENO_ALTO = 1710   # el diseño ZPL está dibujado en puntos de 30 a 1740; se achica para entrar en la zona de abajo


def _arriba(v) -> float:
    try:
        return max(0.0, min(70.0, float(v)))
    except (TypeError, ValueError):
        return ARRIBA_MM


def _z(s) -> str:
    """Texto seguro para ^FD (sin ^ ni ~, que son comandos)."""
    return re.sub(r"[\^~\r\n]+", " ", str(s or "")).strip()


def _achicar(texto: str, entra: int, minimo: float) -> float:
    """Factor de tamaño de letra: 1 si el texto tiene hasta `entra` caracteres; si no, proporcional (con un mínimo)."""
    n = len(texto or "")
    return 1.0 if n <= entra else max(minimo, entra / n)


def _datos(r: dict) -> dict:
    """Textos del rótulo (mismo modelo que la "Etiqueta de embalaje" de Odoo), compartidos por ZPL y HTML."""
    rem, cli = r["rem"], r["cli"]
    peso = r.get("peso") or 0
    return {
        "empresa": rem["nombre"], "fecha": r["fecha"], "bulto": f"{r['bulto']} de {r['total']}",
        "peso": f"{peso:.1f}".replace(".", ",") if peso else "", "pedido": r["pedido"] or r["picking"] or "-",
        "picking": r["picking"] if r["pedido"] else "", "obs": r.get("obs", ""),
        "dest": [("Razón social", cli["nombre"]), ("CUIT", cli.get("cuit", "")), ("Dirección", cli.get("calle", "")),
                 ("Ciudad", cli.get("localidad", "")), ("Provincia", cli.get("provincia", "")),
                 ("CP", cli.get("cp", "")), ("Tel.", cli.get("telefono", ""))],
        "expreso": cli.get("expreso") or "SIN EXPRESO", "codigo": cli["codigo"], "url": r["url"],
        # letra más chica si el texto es largo, para que entre en una línea
        "f_exp": _achicar(cli.get("expreso") or "SIN EXPRESO", 15, 0.45),
        "f_rs": _achicar(f"Razón social: {cli['nombre']}", 34, 0.62),
        "tracking": r.get("tracking", ""),
        "rem_izq": [("Razón social", rem["nombre"]), ("CUIT", rem.get("cuit", "")), ("Domicilio", rem.get("calle", ""))],
        "rem_der": [("Teléfono", rem.get("telefono", "")),
                    ("Localidad", " ".join(x for x in (rem.get("localidad", ""), f"(CP {rem['cp']})" if rem.get("cp") else "") if x)),
                    ("Provincia", rem.get("provincia", ""))],
    }


def _zpl_rotulo(r: dict, cfg: dict) -> str:
    d = _datos(r)
    X0, X1, W = MX, PW - MX, PW - 2 * MX
    z = [f"^XA^CI28^PW{PW}^LL{LL}^LH{cfg['x']},{cfg['y']}^MD{cfg['md']}^PON"]
    top = round(_arriba(cfg.get("arriba", ARRIBA_MM)) * DPMM)
    escala = (LL - top - 30) / DISENO_ALTO

    # las y y los altos están en puntos del diseño; _Y/_S los llevan a la zona libre de abajo
    def _Y(v: float) -> int:
        return top + 10 + round((v - 30) * escala)

    def _S(v: float) -> int:
        return max(1, round(v * escala))

    def caja(y, h):
        z.append(f"^FO{X0},{_Y(y)}^GB{W},{_S(h)},3^FS")

    def vline(x, y, h):
        z.append(f"^FO{x},{_Y(y)}^GB3,{_S(h)},3^FS")

    def txt(x, y, alto, texto, ancho=0, lineas=1, centro=False):
        fb = f"^FB{ancho},{lineas},4,{'C' if centro else 'L'}" if ancho else ""
        z.append(f"^FO{x},{_Y(y)}^A0N,{_S(alto)},{_S(alto)}{fb}^FD{_z(texto)}^FS")

    def titulo(y, texto):
        txt(X0 + 14, y + 12, 32, texto)
        z.append(f"^FO{X0},{_Y(y + 52)}^GB{W},2,2^FS")

    # 1) empresa | fecha, bulto, peso, pedido
    y, h, xs = 30, 250, X0 + 600
    caja(y, h)
    vline(xs, y, h)
    txt(X0 + 24, y + 70, 84, d["empresa"], 560, 2)
    txt(xs + 18, y + 22, 32, f"Fecha: {d['fecha']}")
    txt(xs + 18, y + 72, 34, "Bulto:")
    txt(xs + 130, y + 58, 60, d["bulto"])
    txt(xs + 18, y + 132, 32, f"Peso (kg): {d['peso'] or '-'}")
    txt(xs + 18, y + 174, 34, f"#Pedido: {d['pedido']}", X1 - xs - 30, 1)
    if d["picking"]:
        txt(xs + 18, y + 214, 26, d["picking"])
    # 2) FRAGIL | OBS
    y, h, xs = 280, 70, X0 + 250
    caja(y, h)
    vline(xs, y, h)
    txt(X0 + 14, y + 18, 40, "FRAGIL")
    txt(xs + 14, y + 22, 28, f"OBS: {d['obs']}", X1 - xs - 24, 1)
    # 3) destinatario
    y, h = 350, 440
    caja(y, h)
    titulo(y, "DESTINATARIO")
    dest = dict(d["dest"])
    txt(X0 + 18, y + 68 + round(23 * (1 - d["f_rs"])), round(46 * d["f_rs"]), f"Razón social: {dest['Razón social']}", W - 36, 2)
    txt(X0 + 18, y + 172, 36, f"CUIT: {dest['CUIT']}", W - 36, 1)
    txt(X0 + 18, y + 220, 36, f"Dirección: {dest['Dirección']}", W - 36, 2)
    txt(X0 + 18, y + 306, 36, f"Ciudad: {dest['Ciudad']}", W - 36, 1)
    txt(X0 + 18, y + 348, 36, f"Provincia: {dest['Provincia']}", W - 36, 1)
    txt(X0 + 18, y + 390, 36, f"CP: {dest['CP'] or '-'}    Tel.: {dest['Tel.'] or '-'}", W - 36, 1)
    # 4) expreso
    y, h = 790, 170
    caja(y, h)
    titulo(y, "EXPRESO")
    txt(X0, y + 72 + round(43 * (1 - d["f_exp"])), round(86 * d["f_exp"]), d["expreso"], W, 1, centro=True)
    # 5) tracking: código de barras del cliente + QR a su ficha
    y, h = 960, 460
    caja(y, h)
    titulo(y, "TRACKING")
    z.append(f"^FO{X0 + 24},{_Y(y + 80)}^BY4,3,{_S(250)}^BCN,{_S(250)},N,N,N^FD{_z(d['codigo'])}^FS")
    txt(X0 + 24, y + 345, 48, d["codigo"])
    if d["tracking"]:
        txt(X0 + 24, y + 405, 30, f"Seguimiento: {d['tracking']}", 640, 1)
    z.append(f"^FO{X1 - 305},{_Y(y + 70)}^BQN,2,8^FDQA,{_z(d['url'])}^FS")
    # 6) remitente
    y, h, xs = 1420, 320, X0 + 600
    caja(y, h)
    titulo(y, "REMITENTE")
    vline(xs, y + 54, h - 54)
    yy = y + 72
    for k, v in d["rem_izq"]:
        txt(X0 + 18, yy, 32, f"{k}: {v}", 570, 2 if k == "Domicilio" else 1)
        yy += 80 if k == "Domicilio" else 44
    yy = y + 72
    for k, v in d["rem_der"]:
        txt(xs + 18, yy, 32, f"{k}: {v}", X1 - xs - 30, 2)
        yy += 80
    z.append("^XZ")
    return "\n".join(z) + "\n"


def config(db: Session | None = None) -> dict:
    def num(clave: str, defecto: float) -> float:
        try:
            return float((ajustes.leer(clave, db) or "").strip().replace(",", ".") or defecto)
        except ValueError:
            return defecto
    return {"x": max(0, round(num("rotulo_desplaz_x", 0) * DPMM)), "y": max(0, round(num("rotulo_desplaz_y", 0) * DPMM)),
            "md": max(-30, min(30, int(num("rotulo_oscuridad", 0)))), "arriba": _arriba(num("rotulo_arriba_mm", ARRIBA_MM))}


def build_zpl(rotulos: list[dict], cfg: dict | None = None) -> str:
    cfg = cfg or {"x": 0, "y": 0, "md": 0, "arriba": ARRIBA_MM}
    return "".join(_zpl_rotulo(r, cfg) for r in rotulos)


# ───────────────────────── HTML 100 x 150 mm (driver de Windows) ─────────────────────────
def barcode_svg(texto: str, alto_mm: float, modulo_mm: float = 0.42) -> str:
    bits = code128(texto)
    rects, i = [], 0
    while i < len(bits):
        if bits[i] == "1":
            j = i
            while j < len(bits) and bits[j] == "1":
                j += 1
            rects.append(f'<rect x="{i * modulo_mm:.3f}" y="0" width="{(j - i) * modulo_mm:.3f}" height="{alto_mm}"/>')
            i = j
        else:
            i += 1
    w = len(bits) * modulo_mm
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.2f}mm" height="{alto_mm}mm" viewBox="0 0 {w:.3f} {alto_mm}" '
            f'shape-rendering="crispEdges">{"".join(rects)}</svg>')


def qr_svg(texto: str, lado_mm: float) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(texto, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=0)
    s = img.to_string(encoding="unicode")
    return re.sub(r'<svg([^>]*?)width="[^"]*"\s+height="[^"]*"', rf'<svg\1width="{lado_mm}mm" height="{lado_mm}mm"', s, count=1)


CSS_ROTULO = """@page{size:100mm 150mm;margin:0}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,Helvetica,sans-serif;color:#000;background:#fff}
.rot{width:100mm;height:150mm;page-break-after:always;overflow:hidden}
.zona{width:100mm;overflow:hidden}
.cont{padding:2.5mm 3mm;transform-origin:0 0}
.rot:last-child{page-break-after:auto}
.c{border:.3mm solid #000;border-top:0;overflow:hidden}.c:first-child{border-top:.3mm solid #000}
.f{display:flex}.f>div{min-width:0}.bl{border-left:.3mm solid #000}
.t{font-size:2.9mm;font-weight:700;padding:.8mm 1.4mm;border-bottom:.2mm solid #000}
.b{padding:1mm 1.6mm;font-size:3.1mm;line-height:3.9mm}
.emp{flex:1.15;font-size:7mm;font-weight:700;line-height:7.6mm;padding:2.5mm 2mm;display:flex;align-items:center}
.cab{flex:1;font-size:2.9mm;line-height:4.2mm;padding:1.4mm 1.6mm;white-space:nowrap}.cab big{font-size:5.4mm;font-weight:700}
.fr{font-size:3.4mm;font-weight:700;padding:1.2mm 1.6mm;width:22mm}
.obs{flex:1;font-size:2.6mm;padding:1.6mm;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rs{font-size:4mm;font-weight:700;line-height:4.6mm;margin-bottom:.6mm;white-space:nowrap;overflow:hidden}
.exp{text-align:center;font-weight:700;font-size:7.4mm;line-height:10mm;white-space:nowrap;overflow:hidden}
.trk{display:flex;justify-content:space-between;align-items:flex-start;padding:2mm 2mm 1.5mm}
.trk .k{font-size:4mm;font-weight:700;margin-top:1mm}.trk .s{font-size:2.6mm;margin-top:.8mm}
.rem{font-size:2.7mm;line-height:3.5mm}
@media screen{body{background:#e4e4e7}.rot{background:#fff;margin:4mm auto;box-shadow:0 1px 4px #0003}}"""


def _rotulo_html(r: dict) -> str:
    d, e = _datos(r), escape

    def filas(pares):
        return "".join(f"<div><b>{e(k)}:</b> {e(v)}</div>" for k, v in pares)
    dest = dict(d["dest"])
    pick = f' <span style="font-size:2.4mm">· {e(d["picking"])}</span>' if d["picking"] else ""
    seg = f'<div class="s">Seguimiento: {e(d["tracking"])}</div>' if d["tracking"] else ""
    return f"""<div class="rot"><div class="zona"><div class="cont">
<div class="c f"><div class="emp">{e(d['empresa'])}</div><div class="cab bl"><div><b>Fecha:</b> {e(d['fecha'])}</div>
<div><b>Bulto:</b> <big>{e(d['bulto'])}</big></div><div><b>Peso (kg):</b> {e(d['peso'] or '-')}</div>
<div><b>#Pedido:</b> {e(d['pedido'])}{pick}</div></div></div>
<div class="c f"><div class="fr">FRAGIL</div><div class="obs bl">OBS: {e(d['obs'])}</div></div>
<div class="c"><div class="t">DESTINATARIO</div><div class="b"><div class="rs" style="font-size:{4 * d['f_rs']:.2f}mm">Razón social: {e(dest['Razón social'])}</div>
{filas([(k, dest[k]) for k in ('CUIT', 'Dirección', 'Ciudad', 'Provincia')])}
<div><b>CP:</b> {e(dest['CP'] or '-')} &nbsp; <b>Tel.:</b> {e(dest['Tel.'] or '-')}</div></div></div>
<div class="c"><div class="t">EXPRESO</div><div class="exp" style="font-size:{7.4 * d['f_exp']:.2f}mm">{e(d['expreso'])}</div></div>
<div class="c"><div class="t">TRACKING</div><div class="trk"><div>{barcode_svg(d['codigo'], 18)}<div class="k">{e(d['codigo'])}</div>{seg}</div>
{qr_svg(d['url'], 26)}</div></div>
<div class="c"><div class="t">REMITENTE</div><div class="f rem"><div class="b" style="flex:1.1">{filas(d['rem_izq'])}</div>
<div class="b bl" style="flex:1">{filas(d['rem_der'])}</div></div></div>
</div></div></div>"""


CONT_ALTO_MM = 140   # alto del diseño HTML sin achicar (con nombres largos)


def css_zona(arriba: float = ARRIBA_MM) -> str:
    """Los mm de arriba en blanco y el rótulo achicado para entrar en el resto de la etiqueta de 150 mm."""
    arriba = _arriba(arriba)
    alto = 150 - arriba
    k = min(1.0, (alto - 2) / CONT_ALTO_MM)
    return (f".rot{{padding-top:{arriba:g}mm}}.zona{{height:{alto:g}mm}}"
            f".cont{{width:{100 / k:.2f}mm;transform:scale({k:.3f})}}")


def build_html(rotulos: list[dict], titulo: str = "Rótulos", auto_print: bool = True, arriba: float = ARRIBA_MM) -> str:
    js = "<script>window.addEventListener('load',()=>setTimeout(()=>window.print(),300))</script>" if auto_print else ""
    return (f'<!doctype html><html lang="es"><head><meta charset="utf-8"><title>{escape(titulo)}</title>'
            f'<style>{CSS_ROTULO}{css_zona(arriba)}</style></head><body>{"".join(_rotulo_html(r) for r in rotulos)}{js}</body></html>')


# ───────────────────────── planilla A4 de códigos de clientes ─────────────────────────
CSS_PLANILLA = """@page{size:A4;margin:8mm}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:Arial,Helvetica,sans-serif;color:#000}
.grilla{display:grid;grid-template-columns:repeat(3,1fr);gap:0}
.c{height:34.5mm;border:.2mm dashed #999;padding:2mm;display:flex;gap:2mm;align-items:center;break-inside:avoid;overflow:hidden}
.c .i{flex:1;min-width:0}.c .n{font-size:2.9mm;font-weight:700;line-height:3.3mm;max-height:6.6mm;overflow:hidden}
.c .k{font-size:3.2mm;font-weight:700;margin-top:.6mm}.c .e{font-size:2.4mm;color:#333;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"""


def build_planilla(clientes: list[dict], url_base: str, auto_print: bool = True) -> str:
    celdas = "".join(
        f'<div class="c"><div class="i"><div class="n">{escape(c["nombre"])}</div>{barcode_svg(c["codigo"], 9, 0.26)}'
        f'<div class="k">{escape(c["codigo"])}</div><div class="e">{escape(c.get("expreso") or "")}</div></div>'
        f'{qr_svg(f"{url_base}/clientes/{c["id"]}", 20)}</div>' for c in clientes)
    js = "<script>window.addEventListener('load',()=>setTimeout(()=>window.print(),300))</script>" if auto_print else ""
    return (f'<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Códigos de clientes</title>'
            f'<style>{CSS_PLANILLA}</style></head><body><div class="grilla">{celdas}</div>{js}</body></html>')
