"""Sección Recolecciones Odoo: las recolecciones abiertas (WH/PICK) y sus etiquetas en rollo Honeywell, una por unidad."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import requiere
from ..db import get_db
from ..models import Usuario
from ..services import etiquetas_rollo as rollo
from ..services import odoo
from ..services import odoo_recolecciones as svc
from ..templating import render

router = APIRouter(prefix="/recolecciones-odoo")
acceso = requiere("pick_odoo")


@router.get("")
def pagina(request: Request, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return render(request, "recolecciones_odoo.html", u=u, conectado=odoo.configurado(db))


@router.get("/api/lista")
def lista(refrescar: bool = False, todas: bool = False, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    try:
        picks = svc.listar(db, refrescar, todas)
    except odoo.OdooError as e:
        raise HTTPException(502, str(e)) from e
    return {"recolecciones": picks, "total": len(picks), "todas": todas,
            "nuevas": sum(1 for p in picks if p["abierta"] and not p["impresion"])}


def _pick(db: Session, pid: int) -> dict:
    try:
        p = svc.recoleccion(db, pid)
    except odoo.OdooError as e:
        raise HTTPException(502, str(e)) from e
    if not p:
        raise HTTPException(404, "No se encontró la recolección en Odoo")
    return p


def _lista(db: Session, pid: int) -> tuple[dict, list[dict]]:
    p = _pick(db, pid)
    lista = svc.lista_etiquetas(db, p)
    if not lista:
        raise HTTPException(400, f"{p['nombre']} no tiene unidades para etiquetar")
    return p, lista


@router.get("/{pid}/etiquetas-rollo.html")
def etiquetas_rollo_html(pid: int, filas: int = 0, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Rollo Honeywell con diálogo (página de 93 x 25 mm por fila). Sin ?filas (todas) queda registrada la impresión."""
    p, lista = _lista(db, pid)
    if not filas:
        svc.registrar_impresion(db, p, u.id, len(lista))
        registrar(db, u, "imprimió etiquetas", f"{len(lista)} etiquetas de {p['nombre']} ({p['origen']})", seccion="pick_odoo")
    return HTMLResponse(rollo.build_html(lista, rollo.config(db), f"Etiquetas {p['nombre']}", max_filas=max(0, filas)))


class RolloIn(BaseModel):
    filas: int = 0


@router.post("/api/{pid}/imprimir-rollo")
def imprimir_rollo(pid: int, body: RolloIn = RolloIn(), u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    p, lista = _lista(db, pid)
    try:
        r = rollo.imprimir(lista, db, f"Etiquetas {p['nombre']}", max_filas=max(0, body.filas))
    except rollo.ImpresoraError as e:
        raise HTTPException(502, str(e)) from e
    if not body.filas:
        svc.registrar_impresion(db, p, u.id, len(lista))
    return r


@router.get("/{pid}/etiquetas.zpl")
def etiquetas_zpl(pid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    p, lista = _lista(db, pid)
    nombre = p["nombre"].replace("/", "-")
    return Response(rollo.build_zpl(lista, rollo.config(db)).encode("utf-8"), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="etiquetas_{nombre}.zpl"'})
