from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..actividad import registrar
from ..auth import solo_admin, requiere, hash_pin, pin_valido, verificar_pin, accesos_de, accesos_por_rol, FUNCIONES
acceso_cierre = requiere("cierre")
from ..db import get_db, hoy
from ..models import Cierre, Seccion, Usuario, ROLES
from ..services import drive
from ..services.cierre import datos_cierre, excel_cierre, html_cierre, enviar_cierre, mail_configurado
from ..templating import render

router = APIRouter(prefix="/admin")


# ─────────────── Usuarios ───────────────
@router.get("/usuarios")
def usuarios_page(request: Request, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    usuarios = db.scalars(select(Usuario).order_by(Usuario.activo.desc(), Usuario.nombre)).all()
    pin_default = verificar_pin(config.ADMIN_PIN, u.pin_hash) and config.ADMIN_PIN == "1234"
    nombres = {x.slug: f"{x.icono} {x.nombre}" for x in db.scalars(select(Seccion).order_by(Seccion.orden))}
    nombres.update({k: v.split(" (")[0].split(",")[0] for k, v in FUNCIONES.items()})
    resumen = {x.id: sorted(accesos_de(db, x), key=lambda k: list(nombres).index(k) if k in nombres else 99)
               for x in usuarios}
    return render(request, "admin_usuarios.html", u=u, usuarios=usuarios, pin_default=pin_default,
                  resumen=resumen, nombres=nombres)


def _catalogo(db: Session) -> dict:
    return {"secciones": [{"clave": x.slug, "nombre": x.nombre, "icono": x.icono, "activa": x.activa}
                          for x in db.scalars(select(Seccion).order_by(Seccion.orden, Seccion.id))],
            "funciones": [{"clave": k, "nombre": v} for k, v in FUNCIONES.items()]}


@router.get("/api/usuarios/{uid}/accesos")
def ver_accesos(uid: int, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    x = db.get(Usuario, uid)
    if not x:
        raise HTTPException(404)
    return {"id": x.id, "nombre": x.nombre, "rol": x.rol, "es_admin": x.es_admin,
            "personalizado": x.accesos is not None and not x.es_admin,
            "accesos": sorted(accesos_de(db, x)), "por_rol": sorted(accesos_por_rol(db, x.rol)),
            "catalogo": _catalogo(db)}


class AccesosIn(BaseModel):
    accesos: list[str] | None = None  # None = volver a los del rol


@router.post("/api/usuarios/{uid}/accesos")
def guardar_accesos(uid: int, body: AccesosIn, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    x = db.get(Usuario, uid)
    if not x:
        raise HTTPException(404)
    if x.es_admin:
        raise HTTPException(400, "Los usuarios con rol Admin tienen acceso a todo")
    if body.accesos is None:
        x.accesos = None
        detalle = f"{x.nombre}: accesos del rol {ROLES[x.rol]}"
    else:
        validos = {c["clave"] for grupo in _catalogo(db).values() for c in grupo}
        elegidos = [a for a in body.accesos if a in validos]
        x.accesos = ",".join(sorted(set(elegidos)))
        detalle = f"{x.nombre}: {', '.join(sorted(set(elegidos))) or 'sin accesos'}"
    registrar(db, u, "accesos_editados", detalle, commit=False)
    db.commit()
    return {"ok": True, "accesos": sorted(accesos_de(db, x))}


class UsuarioIn(BaseModel):
    nombre: str
    rol: str
    pin: str = ""
    activo: bool = True
    desbloquear: bool = False


def _validar(db: Session, body: UsuarioIn, uid: int | None = None):
    if not body.nombre.strip():
        raise HTTPException(400, "Poné un nombre")
    if body.rol not in ROLES:
        raise HTTPException(400, "Rol inválido")
    if body.pin and not pin_valido(body.pin):
        raise HTTPException(400, "El PIN tiene que ser de 4 a 8 números")
    otro = db.scalar(select(Usuario).where(Usuario.nombre == body.nombre.strip()))
    if otro and otro.id != uid:
        raise HTTPException(400, "Ya existe un usuario con ese nombre")


@router.post("/api/usuarios")
def crear_usuario(body: UsuarioIn, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    _validar(db, body)
    if not body.pin:
        raise HTTPException(400, "Poné un PIN")
    nuevo = Usuario(nombre=body.nombre.strip(), rol=body.rol, pin_hash=hash_pin(body.pin), activo=True)
    db.add(nuevo)
    registrar(db, u, "usuario_creado", f"{nuevo.nombre} ({ROLES[body.rol]})", commit=False)
    db.commit()
    return {"ok": True}


@router.post("/api/usuarios/{uid}")
def editar_usuario(uid: int, body: UsuarioIn, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    x = db.get(Usuario, uid)
    if not x:
        raise HTTPException(404)
    _validar(db, body, uid)
    if x.id == u.id and (body.rol != "admin" or not body.activo):
        raise HTTPException(400, "No podés sacarte el rol de admin ni darte de baja a vos mismo")
    x.nombre, x.rol, x.activo = body.nombre.strip(), body.rol, body.activo
    if body.pin:
        x.pin_hash = hash_pin(body.pin)
    if body.desbloquear:
        x.bloqueado_hasta, x.intentos_fallidos = None, 0
    registrar(db, u, "usuario_editado", x.nombre + (" · PIN cambiado" if body.pin else ""), commit=False)
    db.commit()
    return {"ok": True}


# ─────────────── Secciones ───────────────
@router.get("/secciones")
def secciones_page(request: Request, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    secciones = db.scalars(select(Seccion).order_by(Seccion.orden)).all()
    return render(request, "admin_secciones.html", u=u, secciones=secciones)


class SeccionIn(BaseModel):
    nombre: str
    icono: str = "📦"
    descripcion: str = ""
    orden: int = 0
    activa: bool = True
    roles: list[str] = []


@router.post("/api/secciones/{sid}")
def editar_seccion(sid: int, body: SeccionIn, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    s = db.get(Seccion, sid)
    if not s:
        raise HTTPException(404)
    s.nombre, s.icono, s.descripcion, s.orden = body.nombre.strip(), body.icono.strip()[:10], body.descripcion, body.orden
    s.activa = body.activa
    s.roles = ",".join(sorted({"admin", *[r for r in body.roles if r in ROLES]}))
    registrar(db, u, "seccion_editada", s.nombre, commit=False)
    db.commit()
    return {"ok": True}


# ─────────────── Cierre diario ───────────────
@router.get("/cierre")
def cierre_page(request: Request, u: Usuario = Depends(acceso_cierre), db: Session = Depends(get_db),
                fecha: date | None = None):
    fecha = fecha or hoy()
    historial = db.scalars(select(Cierre).order_by(Cierre.enviado_at.desc()).limit(30)).all()
    return render(request, "admin_cierre.html", u=u, fecha=fecha, historial=historial,
                  mail_ok=mail_configurado(), mail_to=config.MAIL_TO,
                  automatico=config.CIERRE_AUTOMATICO, hora=config.CIERRE_HORA, dias=config.CIERRE_DIAS,
                  google=drive.configurado(), gas_minutos=config.GAS_MINUTOS_ANTES, gas_auto=config.GAS_AUTO)


@router.get("/api/google/config")
def google_config(u: Usuario = Depends(acceso_cierre)):
    cfg = drive.leer_config()
    if not cfg:
        raise HTTPException(502, "No pude leer la configuración del Apps Script")
    return cfg


@router.post("/api/google/informar")
def google_informar(u: Usuario = Depends(acceso_cierre), db: Session = Depends(get_db)):
    try:
        return {"ok": True, **drive.informar_dia(db, u)}
    except drive.DriveError as e:
        raise HTTPException(502, str(e))


@router.post("/api/google/cerrar")
def google_cerrar(u: Usuario = Depends(acceso_cierre), db: Session = Depends(get_db)):
    try:
        r = drive.cerrar_dia(db, u)
    except drive.DriveError as e:
        raise HTTPException(502, str(e))
    db.add(Cierre(fecha=hoy(), ok=bool(r.get("ok")), destinatario=str(r.get("mail", "")), automatico=False,
                  error="" if r.get("ok") else str(r)[:500]))
    db.commit()
    return r


@router.get("/cierre/vista", response_class=HTMLResponse)
def cierre_vista(u: Usuario = Depends(acceso_cierre), db: Session = Depends(get_db), fecha: date | None = None):
    return html_cierre(datos_cierre(db, fecha or hoy()))


@router.get("/cierre/excel")
def cierre_excel(u: Usuario = Depends(acceso_cierre), db: Session = Depends(get_db), fecha: date | None = None):
    fecha = fecha or hoy()
    return Response(excel_cierre(datos_cierre(db, fecha)),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="cierre_{fecha.isoformat()}.xlsx"'})


class EnviarIn(BaseModel):
    fecha: date | None = None
    destinatario: str | None = None


@router.post("/api/cierre/enviar")
def cierre_enviar(body: EnviarIn, u: Usuario = Depends(acceso_cierre), db: Session = Depends(get_db)):
    r = enviar_cierre(db, body.fecha or hoy(), destinatario=(body.destinatario or "").strip() or None)
    registrar(db, u, "cierre_enviado", f"{r.fecha} → {r.destinatario} · {'OK' if r.ok else 'ERROR'}")
    if not r.ok:
        raise HTTPException(400, r.error)
    return {"ok": True}
