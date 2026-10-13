from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import intentar_login, iniciar_sesion, usuario_actual_opcional
from ..db import get_db
from ..models import Usuario
from ..templating import render

router = APIRouter()


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db), u=Depends(usuario_actual_opcional)):
    if u:
        return RedirectResponse("/", status_code=303)
    usuarios = db.scalars(select(Usuario).where(Usuario.activo.is_(True)).order_by(Usuario.nombre)).all()
    return render(request, "login.html", usuarios=usuarios)


@router.post("/login")
def login(request: Request, usuario_id: int = Form(...), pin: str = Form(...), db: Session = Depends(get_db)):
    u = db.get(Usuario, usuario_id)
    if not u:
        return JSONResponse({"ok": False, "error": "Usuario inexistente"}, status_code=400)
    ok, msg = intentar_login(db, u, pin.strip())
    if not ok:
        registrar(db, u, "login_fallido", msg)
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    iniciar_sesion(request, u)
    registrar(db, u, "login")
    return {"ok": True, "redirect": "/"}


@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_db), u=Depends(usuario_actual_opcional)):
    if u:
        registrar(db, u, "logout")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
