"""Ajustes guardados en la base (los carga el admin desde Conexiones). Si no hay, se usa el .env."""
import os
import time

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Ajuste

# clave -> (variable del .env, valor por defecto, es_secreto)
CLAVES = {
    "parkahub_url": ("PARKAHUB_URL", "https://parkahub.pages.dev", False),
    "parkahub_client_id": ("PARKAHUB_CLIENT_ID", "", True),
    "parkahub_client_secret": ("PARKAHUB_CLIENT_SECRET", "", True),
    # Mercado Libre directo: proxy de la app de Costos (token de PARKA.AR + perfil de permisos por clave)
    "meli_url": ("MELI_URL", "https://magontex-costos.pages.dev/api/meli", False),
    "meli_api_key": ("MELI_KEY", "", True),
    "meli_seller_id": ("MELI_SELLER_ID", "", False),   # opcional: si el proxy no devuelve /users/me
    "gemini_api_key": ("GEMINI_API_KEY", "", True),
    "gemini_modelo": ("GEMINI_MODELO", "gemini-2.5-flash", False),
    # Respondedor de preguntas de ML (Gemini). Modo: manual = solo borradores · mixto = las simples salen solas · auto = todas
    "resp_modo": ("RESP_MODO", "manual", False),
    "resp_minutos": ("RESP_MINUTOS", "3", False),
    "resp_firma": ("RESP_FIRMA", "¡Saludos! Equipo PARKA", False),
    "resp_instrucciones": ("RESP_INSTRUCCIONES", "", False),   # políticas propias (envíos, cambios, retiros…)
    "resp_estado": ("RESP_ESTADO", "", False),
    # Odoo: catálogo de artículos y códigos de barra (solo lectura, usuario técnico con API key)
    "odoo_url": ("ODOO_URL", "https://magontex.odoo.com", False),
    "odoo_db": ("ODOO_DB", "oncompetence-magontex-main-28955103", False),
    "odoo_usuario": ("ODOO_USUARIO", "", False),
    "odoo_api_key": ("ODOO_API_KEY", "", True),
    "odoo_sync_minutos": ("ODOO_SYNC_MINUTOS", "60", False),
    # Recolección Ecommerce: Apps Script "Recolección Ecommerce — Backend Drive" (carpeta del día)
    "re_url": ("RE_URL", "", False),
    "re_token": ("RE_TOKEN", "", True),
    # Búsqueda automática: revisa ML + TN cada N minutos, suma las ventas nuevas y avisa cancelaciones
    "re_auto": ("RE_AUTO", "1", False),
    "re_auto_min": ("RE_AUTO_MIN", "3", False),
    "re_auto_estado": ("RE_AUTO_ESTADO", "", False),
    "re_fix_arrastre": ("RE_FIX_ARRASTRE", "", False),   # 1 = ya se deshizo el arrastre de días anteriores
    "re_box_cant": ("RE_BOX_CANT", "5", False),            # BOX: prendas por caja (se habilita al juntar esa cantidad)
    # Tienda Nube: app "depo" del Portal de Partners (OAuth). El token y el id de tienda se completan al autorizar.
    "tn_app_id": ("TN_APP_ID", "45453", False),
    "tn_client_secret": ("TN_CLIENT_SECRET", "", True),
    "tn_access_token": ("TN_ACCESS_TOKEN", "", True),
    "tn_store_id": ("TN_STORE_ID", "", False),
    # App de cambios (Panel de Cambios · PARKA en Cloudflare): clave del panel cargada a mano
    "cambios_url": ("CAMBIOS_URL", "https://parka-cambios.digital1-3ba.workers.dev", False),
    "cambios_clave": ("CAMBIOS_CLAVE", "", True),
    "cambios_estados": ("CAMBIOS_ESTADOS", "Aceptado,Etiqueta enviada,Recibido", False),
    "cambios_marcar_despacho": ("CAMBIOS_MARCAR_DESPACHO", "1", False),
    "tn_marcar_despacho": ("TN_MARCAR_DESPACHO", "1", False),   # 1 = al despachar, la orden pasa a enviada en TN
    # SmartPost (ePresis): en Despacho → Tienda Nube se escanea la etiqueta de SmartPost (nº de guía) y se busca la orden
    "sp_url": ("SP_URL", "https://smartpostecommerce.epresis.com/api/", False),
    "sp_token": ("SP_TOKEN", "", True),
    "sp_panel_url": ("SP_PANEL_URL", "https://smartpostecommerce.epresis.com", False),
    "sp_panel_usuario": ("SP_PANEL_USUARIO", "", False),
    "sp_panel_clave": ("SP_PANEL_CLAVE", "", True),   # contraseña del panel web (Mis Pedidos / Búsqueda de Guía)
    "sp_despacho": ("SP_DESPACHO", "1", False),   # 1 = verificar guías de SmartPost en Despacho (Tienda Nube)
    # Despacho: botón "Avisar por WhatsApp" con los totales del día (abre WhatsApp con el texto escrito)
    "wa_despacho_numero": ("WA_DESPACHO_NUMERO", "5491156053000", False),
    "wa_despacho_nombre": ("WA_DESPACHO_NOMBRE", "Sr. Gabriel", False),
    # Etiquetas en rollo: Honeywell PC42t Plus (ZPL), rollo 87 mm, 2 x 40x25 mm (calibración validada)
    "rollo_impresora": ("ROLLO_IMPRESORA", "", False),     # nombre en Windows, o IP[:9100]
    "rollo_desplaz_x": ("ROLLO_DESPLAZ_X", "10", False),   # mm, ambas etiquetas (^LH)
    "rollo_desplaz_der": ("ROLLO_DESPLAZ_DER", "8", False),  # mm, solo la etiqueta derecha
    "rollo_desplaz_y": ("ROLLO_DESPLAZ_Y", "0", False),    # mm, vertical
    "rollo_html_x": ("ROLLO_HTML_X", "0", False),          # mm, corrimiento global solo para la impresión por driver
    "rollo_oscuridad": ("ROLLO_OSCURIDAD", "5", False),    # ^MD (-30 a 30)
    "rollo_velocidad": ("ROLLO_VELOCIDAD", "3", False),    # ^PR (pulgadas/seg)
}


# Caché en memoria: cada lectura iba a la base (en la nube, un viaje a Neon por cada llamada a ML).
# Se invalida al guardar desde Conexiones; los "*_estado" no se cachean (cambian solos).
CACHE_SEG = 30
_cache: dict[str, tuple[float, str]] = {}


def olvidar(clave: str | None = None) -> None:
    if clave is None:
        _cache.clear()
    else:
        _cache.pop(clave, None)


def leer(clave: str, db: Session | None = None) -> str:
    c = _cache.get(clave)
    if c and time.monotonic() - c[0] < CACHE_SEG:
        return c[1]
    v = _leer(clave, db)
    if not clave.endswith("_estado"):
        _cache[clave] = (time.monotonic(), v)
    return v


def _leer(clave: str, db: Session | None = None) -> str:
    env, defecto, _ = CLAVES[clave]
    propia = db is None
    db = db or SessionLocal()
    try:
        a = db.get(Ajuste, clave)
        if a and a.valor:
            return a.valor
    except Exception:  # noqa: BLE001 — tabla todavía no creada
        pass
    finally:
        if propia:
            db.close()
    return os.environ.get(env, "") or defecto


def guardar(db: Session, clave: str, valor: str) -> None:
    if clave not in CLAVES:
        raise KeyError(clave)
    a = db.get(Ajuste, clave)
    if a is None:
        db.add(Ajuste(clave=clave, valor=valor))
    else:
        a.valor = valor
    db.commit()
    olvidar(clave)


def enmascarar(v: str) -> str:
    return "" if not v else ("•" * 6 + v[-4:] if len(v) > 8 else "•" * len(v))


def resumen(db: Session) -> dict:
    """Para la pantalla: los secretos solo se muestran enmascarados."""
    out = {}
    for k, (_, _, secreto) in CLAVES.items():
        v = leer(k, db)
        out[k] = {"valor": enmascarar(v) if secreto else v, "cargado": bool(v), "secreto": secreto}
    return out
