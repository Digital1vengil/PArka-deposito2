"""SmartPost: envíos manuales (igual que en el scanner) + correos del cierre."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import requiere_seccion, SinPermiso, puede
from ..db import get_db, hoy, ahora
from ..models import EnvioManual, Usuario
from ..services import drive
from ..templating import render

router = APIRouter(prefix="/smartpost")
acceso = requiere_seccion("smartpost")


def env_json(e: EnvioManual) -> dict:
    return {"id": e.id, "plataforma": e.plataforma, "nombre": e.nombre, "articulo": e.articulo,
            "numero": e.numero, "hora": e.hora, "drive_ok": e.drive_ok, "drive_error": e.drive_error,
            "usuario": e.usuario.nombre if e.usuario else ""}


@router.get("")
def pagina(request: Request):
    """La sección SmartPost se quitó: los envíos manuales se cargan en Despacho (scanner)."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/despacho", status_code=303)


@router.get("/api/lista")
def lista(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    envios = db.scalars(select(EnvioManual).where(EnvioManual.fecha == hoy()).order_by(EnvioManual.id.desc())).all()
    return {"envios": [env_json(e) for e in envios], "es_admin": puede(db, u, "cierre")}


class EnvioIn(BaseModel):
    plataforma: str = "ML"
    nombre: str
    articulo: str
    numero: str


@router.post("/api/envios")
def agregar(body: EnvioIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    nombre, articulo, numero = body.nombre.strip(), body.articulo.strip(), body.numero.strip()
    plat = "TN" if body.plataforma.upper() == "TN" else "ML"
    if not nombre or not articulo:
        raise HTTPException(400, "Completá nombre y artículo")
    if not numero:
        raise HTTPException(400, "Falta el número de venta" if plat == "ML" else "Falta el número de orden")
    e = EnvioManual(fecha=hoy(), hora=ahora().strftime("%H:%M"), plataforma=plat, nombre=nombre[:120],
                    articulo=articulo[:200], numero=numero[:60], usuario_id=u.id)
    db.add(e)
    db.commit()
    registrar(db, u, "envio_manual", f"{plat} · {nombre} · {articulo} · {numero}", "smartpost")
    ok = drive.enviar_manual(db, e) if drive.configurado() else False
    return {"ok": True, "drive_ok": ok, "drive_error": e.drive_error, "envio": env_json(e)}


@router.post("/api/envios/{eid}/reintentar")
def reintentar(eid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    e = db.get(EnvioManual, eid)
    if not e:
        raise HTTPException(404)
    if not drive.enviar_manual(db, e):
        raise HTTPException(502, e.drive_error or "No se pudo guardar en Drive")
    return {"ok": True}


@router.post("/api/envios/{eid}/borrar")
def borrar(eid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    if not puede(db, u, "cierre"):
        raise SinPermiso()
    e = db.get(EnvioManual, eid)
    if not e:
        raise HTTPException(404)
    registrar(db, u, "envio_manual_borrado", f"{e.plataforma} · {e.nombre} · {e.numero}", "smartpost", commit=False)
    db.delete(e)
    db.commit()
    return {"ok": True, "aviso": "Si ya estaba en Drive, quedó anotado en el LogDia del Sheet" if e.drive_ok else ""}


@router.get("/api/config")
def config_get(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    if not puede(db, u, "cierre"):
        raise SinPermiso()
    cfg = drive.leer_config()
    if not cfg:
        raise HTTPException(502, "No pude leer los correos del Apps Script")
    return cfg


class ConfigIn(BaseModel):
    mail: str
    copia: str = ""
    hora: int = 17


@router.post("/api/config")
def config_set(body: ConfigIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    if not puede(db, u, "cierre"):
        raise SinPermiso()
    if "@" not in body.mail:
        raise HTTPException(400, "El correo principal no es válido")
    if body.copia and "@" not in body.copia:
        raise HTTPException(400, "El correo adicional no es válido")
    if not 0 <= body.hora <= 23:
        raise HTTPException(400, "La hora tiene que ser de 0 a 23")
    try:
        r = drive.guardar_config(body.mail.strip(), body.copia.strip(), body.hora)
    except drive.DriveError as e:
        raise HTTPException(502, str(e))
    registrar(db, u, "correos_cierre", f"{body.mail} / {body.copia or '-'} / {body.hora} h")
    return {"ok": True, **(r if isinstance(r, dict) else {})}


@router.post("/api/cierre")
def cierre(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    if not puede(db, u, "cierre"):
        raise SinPermiso()
    try:
        return drive.cerrar_dia(db, u)
    except drive.DriveError as e:
        raise HTTPException(502, str(e))
