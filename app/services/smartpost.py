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


def usar_en_despacho(db=None) -> bool:
    """Despacho → Tienda Nube usa SmartPost (token de la API y/o usuario del panel) salvo que esté apagado."""
    return (configurado(db) or panel_configurado(db)) and (ajustes.leer("sp_despacho", db) or "1") != "0"


def olvidar():
    with _lock:
        _cache.clear()
    try:
        olvidar_panel()
    except NameError:
        pass


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
    if panel_configurado():
        try:
            pg = guia_en_panel(codigo)
        except SPError:
            pg = None
        if pg and pg.get("orden"):
            res = {"encontrada": True, "guia": pg["guia"], "estado": pg["estado"], "nombre": "", "remitos": [],
                   "numero": pg["orden"], "via": "panel"}
            with _lock:
                _cache[clave] = (time.monotonic(), res)
            return res
        if pg:
            return {"encontrada": True, "guia": pg["guia"], "estado": pg["estado"], "nombre": pg.get("destinatario", ""),
                    "remitos": [pg.get("remito", "")], "numero": "", "via": "", "sin_orden": True}
    if not configurado():
        return {"encontrada": False, "guia": codigo, "estado": "", "nombre": "", "remitos": [], "numero": "", "via": ""}
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


# ─────────────── Panel web de SmartPost (usuario + contraseña) ───────────────
# La API pública no dice a qué orden de Tienda Nube corresponde cada guía. Esa relación está en el panel:
#   • Tiendanube → Mis Pedidos  (POST /tiendanube/mis/pedidos/list): nº de orden TN ↔ nº de guía (retiro_id), estado
#   • Guías → Búsqueda de Guía  (POST /guias/filter, fecha_remito "dd/mm/aaaa - dd/mm/aaaa"): guías del día
# La app entra con el usuario y la contraseña que se cargan en Conexiones → SmartPost (secreto en la base).
import http.cookiejar
import urllib.parse
from datetime import timedelta

PANEL_TIMEOUT = 25
PANEL_CACHE_SEG = 120
_panel = {"opener": None, "csrf": "", "ts": 0.0}
_panel_cache: dict[str, tuple[float, list]] = {}
_panel_lock = threading.Lock()

# Estados de SmartPost en que la guía ya salió o terminó (no va en la lista para despachar)
FINALES = re.compile(r"ENTREG|CANCEL|ANULAD|DEVUEL|TRANSITO|TRÁNSITO|DISTRIBU|CAMINO|RETIRAD|RENDID|VISITA|SINIESTR|EXTRAV|RECHAZ", re.I)


def panel_configurado(db=None) -> bool:
    return bool((ajustes.leer("sp_panel_usuario", db) or "").strip() and (ajustes.leer("sp_panel_clave", db) or "").strip())


def _panel_base() -> str:
    return (ajustes.leer("sp_panel_url") or "https://smartpostecommerce.epresis.com").strip().rstrip("/")


def _csrf_de(html: str) -> str:
    m = re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)', html) \
        or re.search(r'name=["\']_token["\'][^>]*value=["\']([^"\']+)', html) \
        or re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']_token["\']', html)
    return m.group(1) if m else ""


def _pedir_panel(opener, metodo: str, ruta: str, datos: dict | None = None, ajax: bool = False):
    url = _panel_base() + ruta
    h = {"User-Agent": "Mozilla/5.0 (PARKA Deposito)", "Accept": "application/json" if ajax else "text/html"}
    body = None
    if datos is not None:
        body = urllib.parse.urlencode(datos).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if ajax:
        h["X-Requested-With"] = "XMLHttpRequest"
    try:
        with opener.open(urllib.request.Request(url, data=body, method=metodo, headers=h), timeout=PANEL_TIMEOUT) as r:
            return r.status, r.geturl(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SPError(f"No se pudo conectar con el panel de SmartPost ({getattr(e, 'reason', e)}).") from None


def _login():
    usuario = (ajustes.leer("sp_panel_usuario") or "").strip()
    clave = (ajustes.leer("sp_panel_clave") or "").strip()
    if not usuario or not clave:
        raise SPError("Falta el usuario y la contraseña del panel de SmartPost (Conexiones → SmartPost).")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    st, _, html = _pedir_panel(opener, "GET", "/login")
    tok = _csrf_de(html)
    if not tok:
        raise SPError("No se pudo abrir el login del panel de SmartPost.")
    st, url, html = _pedir_panel(opener, "POST", "/login", {"_token": tok, "username": usuario, "password": clave})
    if st >= 400 or urllib.parse.urlparse(url).path.rstrip("/").endswith("/login"):
        raise SPError("SmartPost no aceptó el usuario o la contraseña del panel.")
    csrf = _csrf_de(html)
    if not csrf:
        _, _, h2 = _pedir_panel(opener, "GET", "/tiendanube/mis/pedidos")
        csrf = _csrf_de(h2)
    _panel.update(opener=opener, csrf=csrf, ts=time.monotonic())


def _panel_json(ruta: str, datos: dict) -> dict:
    """POST al panel con la sesión abierta; si venció, vuelve a entrar una vez."""
    with _panel_lock:
        for intento in range(2):
            if _panel["opener"] is None:
                _login()
            st, url, txt = _pedir_panel(_panel["opener"], "POST", ruta, dict(datos, _token=_panel["csrf"]), ajax=True)
            vencida = st in (401, 419) or urllib.parse.urlparse(url).path.rstrip("/").endswith("/login") \
                or (st == 200 and txt.lstrip().startswith("<"))
            if vencida and intento == 0:
                _panel.update(opener=None, csrf="")
                continue
            if st >= 400:
                raise SPError(f"El panel de SmartPost respondió {st}.")
            try:
                return json.loads(txt)
            except ValueError:
                raise SPError("El panel de SmartPost devolvió algo que no es JSON.") from None
    raise SPError("No se pudo abrir sesión en el panel de SmartPost.")


def olvidar_panel():
    with _panel_lock:
        _panel.update(opener=None, csrf="", ts=0.0)
    _panel_cache.clear()


def _cacheado(clave: str, fn, refrescar: bool):
    c = _panel_cache.get(clave)
    if not refrescar and c and time.monotonic() - c[0] < PANEL_CACHE_SEG:
        return c[1]
    data = fn()
    _panel_cache[clave] = (time.monotonic(), data)
    return data


def pedidos_tn(refrescar: bool = False, cantidad: int = 300) -> list[dict]:
    """Tiendanube → Mis Pedidos: [{orden, guia, estado, cancelado, empaquetado, creado}] (más nuevos primero)."""
    def leer():
        filtros = {"Orden": "", "EstadoOrden": "", "Guia": "", "ClienteFiltro": "", "empaquetado": "",
                   "Se_Imprimió_la_etiqueta": "", "estado": "", "fechaCreada": "", "fechaModificada": "", "tiendas": ""}
        j = _panel_json("/tiendanube/mis/pedidos/list",
                        dict(filtros, limit=cantidad, offset=0, order="desc", sort="tnb_order_number", search=""))
        out = []
        for x in (j.get("rows") or []) if isinstance(j, dict) else []:
            if not x.get("tnb_order_number"):
                continue
            out.append({"orden": str(x.get("tnb_order_number")), "guia": str(x.get("retiro_id") or ""),
                        "estado": str(x.get("estadoNombre") or x.get("tnb_estado_desc") or ""),
                        "cancelado": bool(x.get("cancelado")), "empaquetado": bool(x.get("empaquetado")),
                        "creado": str(x.get("created_at") or "")})
        return out
    return _cacheado("pedidos", leer, refrescar)


def guias_del_dia(dia=None, refrescar: bool = False) -> list[dict]:
    """Guías → Búsqueda de Guía con fecha de remito = `dia` (hoy por defecto)."""
    from ..db import hoy
    dia = dia or hoy()
    f = dia.strftime("%d/%m/%Y")

    def leer():
        vacios = dict.fromkeys((
            "cliente_id sucursal_id tipo_operacion_id ceco_id is_urgente nroGuia_desde nroGuia_hasta rto_cliente "
            "guia_agente nroPlanilla_desde nroPlanilla_hasta fecha_ultima_planilla nro_constancia fecha_ultimo_estado "
            "fecha_facturacion fecha_pactada fecha_hora_entrega sector deposito_id con_contrareembolso fecha_repactacion "
            "fecha_confirmacion trabajo lote servicio_id is_facturada rendida planilla_rendicion fecha_rendicion "
            "empresa_comprador nombre_comprador calle cuit dni nroSocio provincia localidad cp_comprador celular "
            "contrareembolso nro_precinto fecha_vencimiento fecha_servicio nro_factura_desde nro_factura_hasta canal "
            "nro_pedido impreso id_consulta").split(), "")
        j = _panel_json("/guias/filter", dict(vacios, limit=500, offset=0, sort="id", order="desc",
                                              fecha_remito=f"{f} - {f}"))
        out = []
        for x in (j.get("rows") or []) if isinstance(j, dict) else []:
            out.append({"guia": str(x.get("id") or ""), "remito": str(x.get("remito") or ""),
                        "estado": str(x.get("estado") or ""), "canal": str(x.get("canal") or ""),
                        "destinatario": str(x.get("comprador_apellido_nombre") or x.get("comprador_empresa") or ""),
                        "domicilio": " ".join(str(x.get(k) or "") for k in ("domicilio", "comprador_localidad")).strip(),
                        "sucursal": str(x.get("sucursal_cliente") or ""), "fecha": str(x.get("fecha_hora") or "")})
        return out
    return _cacheado(f"guias:{f}", leer, refrescar)


def pendiente(estado: str) -> bool:
    """La guía todavía no salió (p. ej. "SOLICITUD CONFIRMADA", "PEND-…")."""
    return not FINALES.search(estado or "")


def probar_panel() -> dict:
    olvidar_panel()
    p = pedidos_tn(refrescar=True, cantidad=50)
    return {"pedidos": len(p), "ultimo": p[0]["orden"] if p else ""}


def guia_en_panel(codigo: str) -> dict | None:
    """Nº de guía escaneado → {orden?, guia, estado, destinatario?, remito?} según el panel (Mis Pedidos / guías de hoy)."""
    cod = str(codigo or "").strip().lstrip("0")
    if not cod or not panel_configurado():
        return None
    for p in pedidos_tn():
        if p["guia"].lstrip("0") == cod:
            return {"orden": p["orden"], "guia": p["guia"], "estado": p["estado"], "cancelado": p["cancelado"]}
    for g in guias_del_dia():
        if g["guia"].lstrip("0") == cod:
            return {"orden": "", "guia": g["guia"], "estado": g["estado"], "destinatario": g["destinatario"],
                    "remito": g["remito"], "canal": g["canal"]}
    return None
