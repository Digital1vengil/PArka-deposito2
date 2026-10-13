"""Sección Mercado Libre: ventas, preguntas (con el respondedor de Gemini), devoluciones y reputación.

Ventas, preguntas sin responder (con la sugerencia de ParkaHub), devoluciones por motivo/SKU, reputación,
y búsqueda de venta / publicación / envío. Todo pasa por services/parkahub.py: no hay token propio de ML. Sin ParkaHub, si está cargada la clave
del proxy de Costos (Conexiones → Mercado Libre directo), las mismas consultas van directo a ML (services/meli.py).
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import requiere
from ..db import get_db
from ..models import PreguntaML, Usuario
from ..services import parkahub, respondedor
from ..templating import render

router = APIRouter(prefix="/ml")
acceso = requiere("ml")


def _leer(fn, *a, **k):
    try:
        return fn(*a, **k)
    except parkahub.ParkaHubError as e:
        raise HTTPException(400, str(e)) from None


@router.get("")
def pagina(request: Request, u: Usuario = Depends(acceso)):
    return render(request, "mercadolibre.html", u=u, conectado=parkahub.disponible(), fuente=parkahub.fuente())


@router.get("/api/resumen")
def resumen(u: Usuario = Depends(acceso)):
    """Lo de arriba de la pantalla. Cada parte por separado: si una falla, las demás se muestran igual."""
    out, errores = {}, {}
    for clave, fn in (("estado", parkahub.estado), ("reputacion", parkahub.reputacion),
                      ("ventas_hoy", lambda: parkahub.ventas_recientes(0, 50)),
                      ("preguntas", lambda: parkahub.preguntas_sin_responder(50))):
        try:
            out[clave] = fn()
        except parkahub.ParkaHubError as e:
            errores[clave] = str(e)
    out["errores"] = errores
    return out


@router.get("/api/ventas")
def ventas(dias: int = 0, u: Usuario = Depends(acceso)):
    return _leer(parkahub.ventas_recientes, max(0, min(dias, 30)), 50)


@router.get("/api/preguntas")
def preguntas(u: Usuario = Depends(acceso)):
    return {"pendientes": _leer(parkahub.preguntas_sin_responder, 100),
            "respondidas": _leer(parkahub.preguntas_respondidas, 30)}


@router.get("/api/devoluciones")
def devoluciones(dias: int = 30, u: Usuario = Depends(acceso)):
    return _leer(parkahub.devoluciones_ml, max(1, min(dias, 180)))


@router.get("/api/buscar")
def buscar(q: str, u: Usuario = Depends(acceso)):
    """Adivina qué es: MLA… → publicación; número largo → venta (y si no, envío); texto → publicaciones."""
    t = (q or "").strip()
    if not t:
        raise HTTPException(400, "Escribí algo para buscar")
    tu = t.upper().replace("-", "")
    if tu.startswith("MLA") and tu[3:].isdigit():
        return {"tipo": "publicacion", "dato": _leer(parkahub.ver_publicacion, tu)}
    dig = "".join(c for c in t if c.isdigit())
    if dig and len(dig) >= 8 and len(dig) == len(t.replace(" ", "")):
        try:
            return {"tipo": "venta", "dato": parkahub.buscar_venta(dig)}
        except parkahub.ParkaHubError:
            return {"tipo": "envio", "dato": _leer(parkahub.estado_envio, dig)}
    return {"tipo": "publicaciones", "dato": _leer(parkahub.buscar_publicaciones, t)}


# ─────────────── Respondedor de preguntas (Gemini) ───────────────

def _resp(fn, *a, **k):
    try:
        return fn(*a, **k)
    except respondedor.RespondedorError as e:
        raise HTTPException(400, str(e)) from None


def _pregunta(db: Session, pid: int) -> PreguntaML:
    p = db.get(PreguntaML, pid)
    if not p:
        raise HTTPException(404, "No encontré esa pregunta")
    return p


@router.get("/api/respondedor")
def respondedor_estado(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return {"config": respondedor.config(db), **respondedor.lista(db)}


class ConfigIn(BaseModel):
    modo: str | None = None
    minutos: int | None = None
    firma: str | None = None
    instrucciones: str | None = None


@router.post("/api/respondedor/config")
def respondedor_config(body: ConfigIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    if not u.es_admin:
        raise HTTPException(403, "Solo un admin cambia el respondedor")
    datos = body.model_dump(exclude_none=True)
    antes = respondedor.modo(db)
    cfg = _resp(respondedor.guardar_config, db, datos)
    if cfg["modo"] != antes:
        registrar(db, u, "respondedor_modo", f"{respondedor.MODOS[antes]} → {respondedor.MODOS[cfg['modo']]}", seccion="ml")
    return cfg


@router.post("/api/respondedor/revisar")
def respondedor_revisar(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    r = respondedor.sincronizar(db)
    if not r.get("ok") and r.get("error") != "Ya se está revisando":
        raise HTTPException(400, r.get("error") or "No se pudo revisar")
    return {"ok": r.get("ok"), "vuelta": r, **respondedor.lista(db), "config": respondedor.config(db)}


class RedactarIn(BaseModel):
    indicacion: str = ""


@router.post("/api/preguntas/{pid}/redactar")
def pregunta_redactar(pid: int, body: RedactarIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    p = _pregunta(db, pid)
    if p.estado in ("publicada", "respondida_ml"):
        raise HTTPException(400, "Esa pregunta ya está respondida")
    p.intentos = 0
    _resp(respondedor.redactar, db, p, body.indicacion)
    return respondedor.a_json(p, respondedor.modo(db))


class PublicarIn(BaseModel):
    texto: str


@router.post("/api/preguntas/{pid}/publicar")
def pregunta_publicar(pid: int, body: PublicarIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    p = _pregunta(db, pid)
    _resp(respondedor.publicar, db, p, body.texto, u)
    return respondedor.a_json(p, respondedor.modo(db))


@router.post("/api/preguntas/{pid}/descartar")
def pregunta_descartar(pid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Saca el borrador de la lista (en ML sigue sin responder: se contesta desde ML)."""
    p = _pregunta(db, pid)
    if p.estado in PENDIENTES_UI:
        p.estado = "descartada"
        db.commit()
    return {"ok": True}


PENDIENTES_UI = respondedor.PENDIENTES
