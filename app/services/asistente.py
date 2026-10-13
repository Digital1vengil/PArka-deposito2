"""Asistente de PARKA con Google Gemini.

Responde preguntas del día a día (ventas, publicaciones, preguntas de compradores, devoluciones,
envíos, despacho, tareas) consultando ParkaHub (Mercado Libre) y la base de esta app.
Es de SOLO LECTURA: no escribe en ML ni cambia nada de la app; propone y redacta, la persona decide.

Se habla con Gemini por su API REST (sin librerías extra: funciona igual en el ejecutable de Windows).
"""
import json
import logging
import urllib.error
import urllib.request
from datetime import timedelta
from typing import Callable

from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session

from . import ajustes, parkahub
from ..db import hoy, ahora
from ..models import Articulo, ChatMensaje, Conteo, Devolucion, Lote, Paquete, Tarea, Usuario

log = logging.getLogger("parka.asistente")
API = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
MAX_PASOS = 6
MAX_HISTORIAL = 20
MAX_SALIDA = 12000  # caracteres que se le devuelven a Gemini por consulta

SISTEMA = """Sos ParkIA, el asistente de PARKA (indumentaria de abrigo, Magontex, Buenos Aires) dentro de la app PARKA Depósito.
Si te preguntan quién sos, decí que sos ParkIA. Ayudás al equipo con Mercado Libre, Tienda Nube, despacho, devoluciones, conteos y tareas.

Reglas:
- Español rioplatense (vos), claro y breve. Listas cortas cuando ordenan.
- Para cualquier dato concreto (ventas, stock, estado de un envío, preguntas, devoluciones, tareas) USÁ LAS
  HERRAMIENTAS. Nunca inventes números, talles, precios ni stock. Si una herramienta falla, decilo tal cual y
  sugerí dónde verificarlo (panel de ML, ParkaHub).
- Las publicaciones de PARKA se reutilizan: el MODELO vivo de la publicación es la verdad actual; para analizar
  una venta pasada mirá el SKU de la venta, no el de la publicación.
- Sos de solo lectura: no publicás respuestas en ML, no cambiás precios ni promos, no cargás nada en la app.
  Si te piden una acción, explicá cómo hacerla (qué sección o botón) o redactá el texto listo para copiar.
- Respuestas a compradores: tono cercano, sin prometer lo que no está en los datos, firmadas "¡Saludos! Equipo PARKA".
- Cuando cites una venta o publicación, poné el número (venta / MLA).
- No pidas ni repitas contraseñas, tokens o claves.
"""


class AsistenteError(Exception):
    pass


# ─────────────────────────── Herramientas ───────────────────────────

def _local_resumen(db: Session, args: dict) -> dict:
    d = hoy()
    lotes = db.scalars(select(Lote).where(Lote.fecha == d)).all()
    por_tipo = {}
    for l in lotes:
        tot = db.scalar(select(func.count()).where(Paquete.lote_id == l.id)) or 0
        desp = db.scalar(select(func.count()).where(Paquete.lote_id == l.id, Paquete.estado != "pendiente")) or 0
        t = por_tipo.setdefault(l.tipo, {"paquetes": 0, "despachados": 0})
        t["paquetes"] += tot
        t["despachados"] += desp
    return {
        "fecha": d.isoformat(),
        "despacho_en_app": por_tipo or "sin lotes cargados hoy en la app (el despacho puede estar en el scanner/Drive)",
        "devoluciones_hoy": db.scalar(select(func.count()).where(Devolucion.fecha == d)) or 0,
        "conteos_hoy": db.scalar(select(func.count()).where(Conteo.fecha == d)) or 0,
        "tareas_abiertas": db.scalar(select(func.count()).where(Tarea.estado != "hecha")) or 0,
        "tareas_vencidas": db.scalar(select(func.count()).where(Tarea.estado != "hecha", Tarea.vence < d)) or 0,
    }


def _local_tareas(db: Session, args: dict) -> list:
    q = select(Tarea).where(Tarea.estado != "hecha").order_by(Tarea.prioridad.desc(), Tarea.vence, Tarea.creada_at)
    if args.get("seccion"):
        q = q.where(Tarea.seccion_slug == args["seccion"])
    out = []
    for t in db.scalars(q.limit(40)).all():
        asignado = db.get(Usuario, t.asignado_a_id).nombre if t.asignado_a_id else None
        out.append({"id": t.id, "seccion": t.seccion_slug, "titulo": t.titulo, "estado": t.estado,
                    "prioridad": t.prioridad, "vence": t.vence.isoformat() if t.vence else None,
                    "asignado_a": asignado, "detalle": (t.detalle or "")[:200]})
    return out


def _local_articulo(db: Session, args: dict) -> list:
    texto = str(args.get("texto") or "").strip()
    if not texto:
        return []
    cond = [or_(Articulo.nombre.ilike(f"%{p}%"), Articulo.sku.ilike(f"%{p}%"), Articulo.codigo_barras == p)
            for p in texto.split()]
    return [{"codigo_barras": a.codigo_barras, "sku": a.sku, "nombre": a.nombre, "color": a.color, "talle": a.talle}
            for a in db.scalars(select(Articulo).where(*cond).limit(30)).all()]


def _local_devoluciones(db: Session, args: dict) -> list:
    dias = int(args.get("dias") or 7)
    desde = hoy() - timedelta(days=dias)
    out = []
    for dv in db.scalars(select(Devolucion).where(Devolucion.fecha >= desde).order_by(Devolucion.id.desc()).limit(30)).all():
        items = [{"articulo": i.articulo, "color": i.color, "talle": i.talle, "estado": i.estado, "cantidad": i.cantidad}
                 for i in getattr(dv, "items", [])]
        out.append({"numero": dv.id, "fecha": dv.fecha.isoformat(), "items": items,
                    "drive_ok": getattr(dv, "drive_ok", None)})
    return out


# nombre -> (descripción, parámetros JSON schema, función(db, args))
HERRAMIENTAS: dict[str, tuple[str, dict, Callable]] = {
    "buscar_venta": (
        "Trae una venta de Mercado Libre por su número: artículos, SKU, talle/color, comprador (apodo), estado y estado del envío.",
        {"type": "object", "properties": {"numero": {"type": "string", "description": "Número de venta de ML"}}, "required": ["numero"]},
        lambda db, a: parkahub.buscar_venta(a.get("numero", ""))),
    "ver_publicacion": (
        "Trae una publicación de ML por MLA: título, precio, estado, modelo, stock por color/talle.",
        {"type": "object", "properties": {"mla": {"type": "string", "description": "Ej. MLA1234567890"}}, "required": ["mla"]},
        lambda db, a: parkahub.ver_publicacion(a.get("mla", ""))),
    "buscar_publicaciones": (
        "Busca publicaciones de PARKA en ML por modelo o texto. Devuelve MLA, modelo, stock, ventas y visitas de 14 días.",
        {"type": "object", "properties": {"texto": {"type": "string"}}, "required": ["texto"]},
        lambda db, a: parkahub.buscar_publicaciones(a.get("texto", ""))),
    "ventas_recientes": (
        "Ventas de ML desde hace N días (0 = hoy). Máximo 50 por consulta.",
        {"type": "object", "properties": {"dias": {"type": "integer"}, "limite": {"type": "integer"}}},
        lambda db, a: parkahub.ventas_recientes(int(a.get("dias") or 0), int(a.get("limite") or 30))),
    "preguntas_sin_responder": (
        "Preguntas de compradores de ML sin responder, con la publicación y la sugerencia de ParkaHub si hay.",
        {"type": "object", "properties": {"limite": {"type": "integer"}}},
        lambda db, a: parkahub.preguntas_sin_responder(int(a.get("limite") or 20))),
    "devoluciones_ml": (
        "Resumen de devoluciones de ML de los últimos N días: total, por motivo y SKUs más devueltos.",
        {"type": "object", "properties": {"dias": {"type": "integer"}}},
        lambda db, a: parkahub.devoluciones_ml(int(a.get("dias") or 30))),
    "reputacion": (
        "Reputación actual del vendedor en ML (nivel, MercadoLíder, reclamos, demoras, cancelaciones).",
        {"type": "object", "properties": {}},
        lambda db, a: parkahub.reputacion()),
    "estado_envio": (
        "Valida un envío de ML por código/número de envío: si se puede despachar, está cancelado o no corresponde.",
        {"type": "object", "properties": {"codigo": {"type": "string"}}, "required": ["codigo"]},
        lambda db, a: parkahub.estado_envio(a.get("codigo", ""))),
    "estado_parkahub": (
        "Estado de la conexión con ML en ParkaHub y qué tan actualizados están sus datos.",
        {"type": "object", "properties": {}},
        lambda db, a: parkahub.estado()),
    "resumen_del_dia": (
        "Resumen de HOY en esta app: despacho por tipo, devoluciones, conteos, tareas abiertas y vencidas.",
        {"type": "object", "properties": {}}, _local_resumen),
    "tareas_abiertas": (
        "Tareas abiertas del equipo (opcional: sección despacho/devoluciones/conteo/tareas).",
        {"type": "object", "properties": {"seccion": {"type": "string"}}}, _local_tareas),
    "buscar_articulo": (
        "Busca en el catálogo de artículos (código de barras, SKU o nombre).",
        {"type": "object", "properties": {"texto": {"type": "string"}}, "required": ["texto"]}, _local_articulo),
    "devoluciones_cargadas": (
        "Devoluciones cargadas en esta app (sección Devoluciones) en los últimos N días.",
        {"type": "object", "properties": {"dias": {"type": "integer"}}}, _local_devoluciones),
}

# Etiquetas para mostrar en pantalla qué consultó
ETIQUETAS = {
    "buscar_venta": "venta ML", "ver_publicacion": "publicación", "buscar_publicaciones": "publicaciones",
    "ventas_recientes": "ventas", "preguntas_sin_responder": "preguntas", "devoluciones_ml": "devoluciones ML",
    "reputacion": "reputación", "estado_envio": "envío", "estado_parkahub": "ParkaHub",
    "resumen_del_dia": "resumen del día", "tareas_abiertas": "tareas", "buscar_articulo": "artículos",
    "devoluciones_cargadas": "devoluciones app",
}


def ejecutar(db: Session, nombre: str, args: dict) -> str:
    h = HERRAMIENTAS.get(nombre)
    if not h:
        return json.dumps({"error": f"herramienta desconocida: {nombre}"})
    try:
        r = h[2](db, args or {})
        s = json.dumps(r, ensure_ascii=False, default=str)
    except parkahub.ParkaHubError as e:
        s = json.dumps({"error": str(e)}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        log.exception("Herramienta %s falló", nombre)
        s = json.dumps({"error": f"falló la consulta: {e}"}, ensure_ascii=False)
    return s[:MAX_SALIDA]


def _declaraciones() -> list[dict]:
    return [{"functionDeclarations": [{"name": n, "description": d, "parameters": p}
                                      for n, (d, p, _) in HERRAMIENTAS.items()]}]


# ─────────────────────────── Gemini (REST) ───────────────────────────

def configurado() -> bool:
    return bool(ajustes.leer("gemini_api_key"))


def _llamar(cuerpo: dict) -> dict:
    """Una llamada a generateContent. Se reemplaza en los tests."""
    clave = ajustes.leer("gemini_api_key")
    if not clave:
        raise AsistenteError("Falta la clave de Gemini (Conexiones → Gemini). Se saca gratis en aistudio.google.com/apikey.")
    url = API.format(modelo=ajustes.leer("gemini_modelo") or "gemini-2.5-flash")
    req = urllib.request.Request(url, data=json.dumps(cuerpo).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": clave})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            pass
        if e.code in (400, 403) and "API key" in msg:
            raise AsistenteError("Gemini rechazó la clave: revisala en Conexiones.") from None
        if e.code == 429:
            raise AsistenteError("Gemini está al límite de uso por ahora: probá en un minuto.") from None
        raise AsistenteError(f"Gemini respondió error {e.code}: {msg[:200]}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AsistenteError(f"No se pudo conectar con Gemini ({getattr(e, 'reason', e)}).") from None


def _contexto(u: Usuario) -> str:
    estado_ph = {"parkahub": "conectado por ParkaHub", "directo": "conectado directo a ML (API de Costos; sin devoluciones por motivo ni cotejo)"}.get(
        parkahub.fuente(), "NO conectado (las herramientas de ML van a fallar)")
    return (f"\nAhora: {ahora():%A %d/%m/%Y %H:%M} (Argentina). Usuario: {u.nombre} (rol {u.rol})."
            f"\nParkaHub (datos de ML): {estado_ph}.")


def responder(db: Session, u: Usuario, mensaje: str) -> dict:
    """Agrega el mensaje al historial, resuelve con Gemini (usando herramientas) y guarda la respuesta."""
    mensaje = (mensaje or "").strip()
    if not mensaje:
        raise AsistenteError("Escribí algo para el asistente.")
    previos = db.scalars(select(ChatMensaje).where(ChatMensaje.usuario_id == u.id)
                         .order_by(ChatMensaje.id.desc()).limit(MAX_HISTORIAL)).all()[::-1]
    contents = [{"role": m.rol, "parts": [{"text": m.texto}]} for m in previos if m.texto]
    contents.append({"role": "user", "parts": [{"text": mensaje}]})
    cuerpo = {"systemInstruction": {"parts": [{"text": SISTEMA + _contexto(u)}]},
              "contents": contents, "tools": _declaraciones(),
              "generationConfig": {"temperature": 0.3}}

    usadas: list[str] = []
    texto = ""
    for _ in range(MAX_PASOS):
        resp = _llamar(cuerpo)
        cand = (resp.get("candidates") or [{}])[0]
        partes = (cand.get("content") or {}).get("parts") or []
        llamadas = [p["functionCall"] for p in partes if "functionCall" in p]
        if not llamadas:
            texto = "".join(p.get("text", "") for p in partes).strip()
            if not texto and cand.get("finishReason") == "SAFETY":
                texto = "No puedo responder eso."
            break
        contents.append({"role": "model", "parts": partes})
        respuestas = []
        for ll in llamadas:
            nombre = ll.get("name", "")
            usadas.append(nombre)
            salida = ejecutar(db, nombre, ll.get("args") or {})
            respuestas.append({"functionResponse": {"name": nombre, "response": {"resultado": salida}}})
        contents.append({"role": "user", "parts": respuestas})
    else:
        texto = "Necesité demasiadas consultas para esto. Probá con una pregunta más puntual."
    texto = texto or "(sin respuesta)"

    etiquetas = ", ".join(dict.fromkeys(ETIQUETAS.get(n, n) for n in usadas))
    db.add(ChatMensaje(usuario_id=u.id, rol="user", texto=mensaje))
    db.add(ChatMensaje(usuario_id=u.id, rol="model", texto=texto, herramientas=etiquetas))
    db.commit()
    return {"texto": texto, "consultas": etiquetas}


def historial(db: Session, u: Usuario, limite: int = 60) -> list[dict]:
    ms = db.scalars(select(ChatMensaje).where(ChatMensaje.usuario_id == u.id)
                    .order_by(ChatMensaje.id.desc()).limit(limite)).all()[::-1]
    return [{"rol": m.rol, "texto": m.texto, "consultas": m.herramientas, "ts": m.ts.strftime("%d/%m %H:%M")} for m in ms]


def borrar_historial(db: Session, u: Usuario) -> None:
    for m in db.scalars(select(ChatMensaje).where(ChatMensaje.usuario_id == u.id)).all():
        db.delete(m)
    db.commit()
