"""Sección Cambios: lo que hay que mandarle al cliente por los cambios aceptados en la app de cambios.
Se ve qué enviar, a quién y por dónde, si la prenda ya se juntó / controló en Recolección Ecommerce,
y se marca "Despachado" (escaneando CAM-000N o el nº de orden) → en la app de cambios queda Despachado."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import requiere
from ..db import get_db
from ..models import Control, ControlPaquete, Usuario
from ..services import cambios as svc
from ..templating import render

router = APIRouter(prefix="/cambios")
acceso = requiere("cambios")


@router.get("")
def pagina(request: Request, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return render(request, "cambios.html", u=u, conectado=svc.configurado(db), estados=svc.estados_preparar(db),
                  marcar=svc.marcar_activo(db))


def _en_busqueda(db: Session) -> dict[str, ControlPaquete]:
    out = {}
    for cp in db.scalars(select(ControlPaquete).join(Control).where(ControlPaquete.tipo == "cambio")
                         .order_by(ControlPaquete.id)).all():
        out[cp.tracking] = cp
    return out


def _json(s: dict, cp: ControlPaquete | None) -> dict:
    p = svc.a_paquete(s)
    busq = None
    if cp is not None:
        hecho = sum(min(i.buscado, i.cantidad) for i in cp.items)
        total = sum(i.cantidad for i in cp.items)
        busq = {"control_id": cp.control_id, "estado": cp.estado, "juntadas": hecho, "unidades": total,
                "faltante": any(i.faltante for i in cp.items), "cancelado": cp.cancelado}
    return {"id": p["tracking"], "orden": p["venta_id"], "cliente": p["comprador"], "estado": s.get("Estado") or "",
            "envio": p["medio"], "fecha": str(s.get("Fecha") or "")[:16], "items": p["items"],
            "devuelve": str(s.get("Devuelve el cliente") or ""), "localidad": str(s.get("Localidad") or ""),
            "busqueda": busq}


@router.get("/api/lista")
def lista(refrescar: bool = False, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    if not svc.configurado(db):
        return {"conectado": False, "cambios": [], "error": ""}
    try:
        ss = svc.solicitudes(refrescar)
    except svc.CambiosError as e:
        return {"conectado": True, "cambios": [], "error": str(e)}
    prep = set(svc.estados_preparar(db))
    eb = _en_busqueda(db)
    por_desp = [_json(s, eb.get(str(s.get("ID")))) for s in ss if s.get("Estado") in prep]
    desp = [_json(s, eb.get(str(s.get("ID")))) for s in ss if s.get("Estado") in svc.ESTADOS_SALIDOS][-15:]
    pend = sum(1 for s in ss if s.get("Estado") == "Pendiente")
    return {"conectado": True, "error": "", "cambios": por_desp, "despachados": desp, "pendientes_aceptar": pend}


class CodigoIn(BaseModel):
    codigo: str


@router.post("/api/despachar")
def despachar(body: CodigoIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Escanear / escribir CAM-000N o el nº de orden → el cambio queda Despachado en la app de cambios."""
    try:
        s = svc.buscar(body.codigo) or svc.buscar(body.codigo, refrescar=True)
    except svc.CambiosError as e:
        raise HTTPException(502, str(e))
    if not s:
        return {"ok": False, "motivo": "no_existe", "mensaje": f"No hay ningún cambio con {body.codigo}"}
    cid, est = str(s.get("ID")), str(s.get("Estado") or "")
    if est in svc.ESTADOS_CERRADOS:
        return {"ok": False, "motivo": "cancelado", "mensaje": f"⛔ {cid} está {est.upper()}: NO DESPACHAR"}
    if est in svc.ESTADOS_SALIDOS:
        return {"ok": False, "motivo": "ya", "mensaje": f"{cid} ya figura como {est}"}
    if est not in svc.estados_preparar(db):
        return {"ok": False, "motivo": "no_listo", "mensaje": f"{cid} está {est}: todavía no se despacha"}
    cp = _en_busqueda(db).get(cid)
    aviso = ""
    if cp is not None and cp.estado != "completo":
        aviso = "Ojo: este cambio no pasó por la mesa de control"
    if svc.marcar_activo(db):
        try:
            svc.marcar(cid, "Despachado")
        except svc.CambiosError as e:
            raise HTTPException(502, str(e))
    registrar(db, u, "cambio_despachado", f"{cid} · orden {svc.a_paquete(s)['venta_id']} · {s.get('Cliente') or ''}", "cambios")
    return {"ok": True, "id": cid, "cliente": s.get("Cliente") or "", "aviso": aviso,
            "mensaje": f"✅ {cid} despachado" + (" · en la app de cambios quedó Despachado" if svc.marcar_activo(db) else "")}
