"""Conexión con Odoo (XML-RPC, solo lectura) para el catálogo de artículos y códigos de barra.

Datos de conexión: Conexiones → Odoo (o variables ODOO_URL / ODOO_DB / ODOO_USUARIO / ODOO_API_KEY).
Se recomienda un usuario técnico con API key y permisos solo de lectura de Inventario.
"""
from __future__ import annotations

import re
import socket
import xmlrpc.client

from sqlalchemy.orm import Session

from . import ajustes
from ..parsers import normalizar_codigo


class OdooError(Exception):
    pass


CAMPOS = ["id", "default_code", "barcode", "display_name", "name", "categ_id",
          "product_template_variant_value_ids", "qty_available", "active", "write_date"]
CAMPOS_OPCIONALES = ["meli_id"]  # del módulo de integración con Mercado Libre (si el usuario lo puede leer)


class _Transporte(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        c = super().make_connection(host)
        c.timeout = self._timeout
        return c


class _TransporteHttp(xmlrpc.client.Transport):
    def __init__(self, timeout: float):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        c = super().make_connection(host)
        c.timeout = self._timeout
        return c


def datos_conexion(db: Session | None = None) -> dict:
    return {"url": ajustes.leer("odoo_url", db).rstrip("/"), "base": ajustes.leer("odoo_db", db),
            "usuario": ajustes.leer("odoo_usuario", db), "clave": ajustes.leer("odoo_api_key", db)}


def configurado(db: Session | None = None) -> bool:
    d = datos_conexion(db)
    return all(d.values())


class Cliente:
    def __init__(self, url: str, base: str, usuario: str, clave: str, timeout: float = 60):
        self.url, self.base, self.usuario, self.clave, self.timeout = url.rstrip("/"), base, usuario, clave, timeout
        self.uid: int | None = None
        self._campos: list[str] | None = None

    def _proxy(self, ruta: str):
        t = _Transporte(self.timeout) if self.url.startswith("https") else _TransporteHttp(self.timeout)
        return xmlrpc.client.ServerProxy(self.url + ruta, allow_none=True, transport=t)

    def login(self) -> int:
        try:
            uid = self._proxy("/xmlrpc/2/common").authenticate(self.base, self.usuario, self.clave, {})
        except (OSError, socket.timeout) as e:
            raise OdooError(f"No se pudo conectar con Odoo ({self.url}): {e}") from e
        except xmlrpc.client.Fault as e:
            raise OdooError(f"Odoo rechazó el ingreso: {e.faultString.strip().splitlines()[-1]}") from e
        if not uid:
            raise OdooError("Usuario o API key de Odoo incorrectos")
        self.uid = uid
        return uid

    def call(self, modelo: str, metodo: str, *args, **kw):
        if self.uid is None:
            self.login()
        try:
            return self._proxy("/xmlrpc/2/object").execute_kw(self.base, self.uid, self.clave, modelo, metodo,
                                                              list(args), kw)
        except (OSError, socket.timeout) as e:
            raise OdooError(f"Se cortó la conexión con Odoo: {e}") from e
        except xmlrpc.client.Fault as e:
            ultima = (e.faultString or "").strip().splitlines()[-1:] or ["error"]
            raise OdooError(f"Odoo respondió con error: {ultima[0]}") from e

    def campos(self) -> list[str]:
        if self._campos is None:
            try:
                existentes = self.call("product.product", "fields_get", CAMPOS_OPCIONALES, attributes=["type"])
            except OdooError:
                existentes = {}
            self._campos = CAMPOS + [c for c in CAMPOS_OPCIONALES if c in existentes]
        return self._campos


def cliente(db: Session | None = None, timeout: float = 60) -> Cliente:
    d = datos_conexion(db)
    if not all(d.values()):
        raise OdooError("Falta configurar Odoo (Conexiones → Odoo: usuario y API key)")
    return Cliente(d["url"], d["base"], d["usuario"], d["clave"], timeout=timeout)


def probar(db: Session | None = None) -> dict:
    c = cliente(db, timeout=20)
    c.login()
    total = c.call("product.product", "search_count", [["barcode", "!=", False]])
    return {"uid": c.uid, "con_codigo": total}


# ───────────── Lectura y normalización de productos ─────────────
DOMINIO_TODOS = ["|", ["active", "=", True], ["active", "=", False]]


def leer_productos(c: Cliente, dominio: list | None = None, lote: int = 500, limite: int | None = None) -> list[dict]:
    """Lee product.product (activos y archivados) y devuelve dicts listos para la tabla Articulo."""
    dominio = DOMINIO_TODOS if dominio is None else dominio
    crudos, offset = [], 0
    while True:
        n = lote if limite is None else min(lote, limite - len(crudos))
        if n <= 0:
            break
        filas = c.call("product.product", "search_read", dominio, fields=c.campos(), offset=offset,
                       limit=n, order="id", context={"active_test": False})
        crudos += filas
        if len(filas) < n:
            break
        offset += n
    valores = leer_valores(c, {v for p in crudos for v in (p.get("product_template_variant_value_ids") or [])})
    return [d for d in (normalizar(p, valores) for p in crudos) if d]


def leer_valores(c: Cliente, ids: set[int]) -> dict[int, tuple[str, str]]:
    """product.template.attribute.value → {id: (atributo, valor)} p. ej. (\"color\", \"BLACK\")."""
    out: dict[int, tuple[str, str]] = {}
    ids_l = sorted(ids)
    for i in range(0, len(ids_l), 2000):
        for v in c.call("product.template.attribute.value", "read", ids_l[i:i + 2000], fields=["name", "attribute_id"]):
            attr = (v.get("attribute_id") or [0, ""])[1] or ""
            out[v["id"]] = (attr, v.get("name") or "")
    return out


_ATTR_COLOR = ("color", "colour", "colores")
_ATTR_TALLE = ("talle", "talla", "size", "talles")
_ATTR_MARCA = ("brand", "marca")


def nombre_marca(m: str, sku: str = "") -> str:
    ml, su = (m or "").strip().lower(), (sku or "").upper()
    if ml in ("nobrand", "no brand", "sin marca") or (not ml and su.endswith("-NOBR")):
        return "No Brand"
    if ml in ("parka", "parca") or (not ml and su.endswith("-PARK")):
        return "Parka"
    if ml == "puffers" or (not ml and su.endswith("-PUFE")):
        return "Puffers"
    return (m or "").strip().capitalize()


def _txt(v) -> str:
    return "" if v in (None, False) else str(v).strip()


def normalizar(p: dict, valores: dict[int, tuple[str, str]]) -> dict | None:
    sku = _txt(p.get("default_code"))
    codigo = normalizar_codigo(_txt(p.get("barcode")))
    if not sku and not codigo:
        return None
    nombre = re.sub(r"^\[[^\]]*\]\s*", "", _txt(p.get("display_name")) or _txt(p.get("name")))
    articulo = _txt(p.get("name")) or nombre
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", nombre)
    vals = [valores[i] for i in (p.get("product_template_variant_value_ids") or []) if i in valores]
    variante = m.group(2).strip() if m else ", ".join(v for _, v in vals)

    def attr(nombres):
        return next((v for a, v in vals if a.strip().lower() in nombres), "")
    color_nombre, talle, marca = attr(_ATTR_COLOR), attr(_ATTR_TALLE), attr(_ATTR_MARCA)
    partes = sku.split("-")
    color = partes[4] if len(partes) >= 7 else color_nombre
    if len(partes) >= 7 and not talle:
        talle = partes[5]
    categ = p.get("categ_id")
    stock = p.get("qty_available")
    return dict(odoo_id=p["id"], sku=sku[:80], codigo_barras=codigo[:20], nombre=nombre[:200], articulo=articulo[:120],
                variante=variante[:120], color=color[:40], talle=talle[:20], color_nombre=color_nombre[:60],
                marca=nombre_marca(marca, sku)[:40], categoria=(categ[1] if isinstance(categ, list) else "")[:80],
                stock=float(stock) if isinstance(stock, (int, float)) and not isinstance(stock, bool) else None,
                meli_id=_txt(p.get("meli_id"))[:40], inactivo=not p.get("active", True))
