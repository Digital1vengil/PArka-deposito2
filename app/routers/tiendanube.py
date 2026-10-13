"""Tienda Nube: autorización OAuth (Conexiones) y "orden enviada" desde Despacho."""
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import requiere, solo_admin
from ..db import get_db
from ..models import Usuario
from ..services import ajustes
from ..services import tiendanube as tn

router = APIRouter()
acceso_despacho = requiere("despacho")


@router.get("/tiendanube/autorizar")
def autorizar(u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    try:
        return RedirectResponse(tn.url_autorizar(db))
    except tn.TNError as e:
        return RedirectResponse("/admin/conexiones?tn_error=" + urllib.parse.quote(str(e)) + "#tiendanube")


@router.get("/tiendanube/callback")
def callback(code: str = "", u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    """URL de redirección de la app en el Portal de Partners: http://localhost:8100/tiendanube/callback"""
    try:
        r = tn.canjear_codigo(db, code)
    except tn.TNError as e:
        return RedirectResponse("/admin/conexiones?tn_error=" + urllib.parse.quote(str(e)) + "#tiendanube")
    registrar(db, u, "conexiones_guardadas", f"Tienda Nube autorizada (tienda {r['tienda']})")
    return RedirectResponse("/admin/conexiones?tn_ok=" + urllib.parse.quote(r["tienda"]) + "#tiendanube")


class CodigoIn(BaseModel):
    codigo: str


@router.post("/admin/api/conexiones/tiendanube/codigo")
def pegar_codigo(body: CodigoIn, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    try:
        r = tn.canjear_codigo(db, body.codigo)
    except tn.TNError as e:
        return {"ok": False, "mensaje": str(e)}
    registrar(db, u, "conexiones_guardadas", f"Tienda Nube autorizada (tienda {r['tienda']})")
    return {"ok": True, "mensaje": f"Conectado · tienda {r['tienda']}"}


@router.post("/admin/api/conexiones/tiendanube/desconectar")
def desconectar(u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    ajustes.guardar(db, "tn_access_token", "")
    ajustes.guardar(db, "tn_store_id", "")
    registrar(db, u, "conexiones_guardadas", "Tienda Nube desconectada")
    return {"ok": True}


@router.get("/admin/api/conexiones/tiendanube/envios")
def envios(u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    return {"envios": tn.ultimos_envios(db)}


class DespachoIn(BaseModel):
    numero: str


@router.post("/api/tiendanube/despachado")
def despachado(body: DespachoIn, u: Usuario = Depends(acceso_despacho), db: Session = Depends(get_db)):
    """Lo llama el scanner de Despacho después de una salida de Tienda Nube (escaneada o manual)."""
    if not tn.configurado(db) or not tn.marcar_activo(db):
        return {"ok": False, "estado": "apagado", "mensaje": ""}
    r = tn.procesar_envio(db, body.numero, u.id)
    if r["estado"] in ("ok", "error"):
        registrar(db, u, "tn_enviada" if r["ok"] else "tn_enviada_error", r["mensaje"][:200], "despacho")
    return r
