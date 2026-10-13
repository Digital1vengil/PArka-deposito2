"""Login por PIN, sesiones y permisos."""
import hashlib
import hmac
import os
from datetime import timedelta

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import SESION_HORAS, PIN_MAX_INTENTOS, PIN_BLOQUEO_MIN
from .db import get_db, ahora
from .models import Usuario, Seccion


class NoAutenticado(Exception):
    pass


class SinPermiso(Exception):
    pass


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 120_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verificar_pin(pin: str, guardado: str) -> bool:
    try:
        _, salt_hex, dk_hex = guardado.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt_hex), 120_000)
    return hmac.compare_digest(dk.hex(), dk_hex)


def pin_valido(pin: str) -> bool:
    return pin.isdigit() and 4 <= len(pin) <= 8


def intentar_login(db: Session, usuario: Usuario, pin: str) -> tuple[bool, str]:
    """Devuelve (ok, mensaje). Maneja intentos fallidos y bloqueo temporal."""
    now = ahora()
    if not usuario.activo:
        return False, "Usuario dado de baja"
    if usuario.bloqueado_hasta and usuario.bloqueado_hasta > now:
        mins = int((usuario.bloqueado_hasta - now).total_seconds() // 60) + 1
        return False, f"Bloqueado por intentos fallidos. Probá en {mins} min."
    if verificar_pin(pin, usuario.pin_hash):
        usuario.intentos_fallidos = 0
        usuario.bloqueado_hasta = None
        usuario.ultimo_login = now
        db.commit()
        return True, ""
    usuario.intentos_fallidos += 1
    restantes = PIN_MAX_INTENTOS - usuario.intentos_fallidos
    if restantes <= 0:
        usuario.bloqueado_hasta = now + timedelta(minutes=PIN_BLOQUEO_MIN)
        usuario.intentos_fallidos = 0
        db.commit()
        return False, f"PIN incorrecto. Usuario bloqueado {PIN_BLOQUEO_MIN} min."
    db.commit()
    return False, f"PIN incorrecto. Te quedan {restantes} intentos."


def iniciar_sesion(request: Request, usuario: Usuario) -> None:
    request.session.clear()
    request.session["uid"] = usuario.id
    request.session["t"] = ahora().isoformat()


def usuario_actual_opcional(request: Request, db: Session = Depends(get_db)) -> Usuario | None:
    uid = request.session.get("uid")
    t = request.session.get("t")
    if not uid or not t:
        return None
    from datetime import datetime
    try:
        inicio = datetime.fromisoformat(t)
    except ValueError:
        return None
    if ahora() - inicio > timedelta(hours=SESION_HORAS):
        request.session.clear()
        return None
    u = db.get(Usuario, uid)
    if not u or not u.activo:
        request.session.clear()
        return None
    return u


def usuario_actual(u: Usuario | None = Depends(usuario_actual_opcional)) -> Usuario:
    if u is None:
        raise NoAutenticado()
    return u


def solo_admin(u: Usuario = Depends(usuario_actual)) -> Usuario:
    if not u.es_admin:
        raise SinPermiso()
    return u


# Funciones que se pueden habilitar por usuario además de las secciones.
# (Usuarios y Secciones quedan siempre solo para el rol Admin.)
FUNCIONES = {
    "supervision": "Supervisión (actividad del equipo)",
    "cierre": "Cierre del día, correos y borrar envíos SmartPost",
    "asignar_tareas": "Asignar, reabrir y borrar tareas",
    "importar_articulos": "Actualizar el catálogo de artículos",
}


def accesos_por_rol(db: Session, rol: str) -> set[str]:
    todas = db.scalars(select(Seccion)).all()
    if rol == "admin":
        return {s.slug for s in todas} | set(FUNCIONES)
    return {s.slug for s in todas if s.permite(rol)}


def accesos_de(db: Session, u: Usuario) -> set[str]:
    """Lo que puede usar el usuario: todo si es Admin; si no, su lista personalizada o la de su rol."""
    if u.es_admin:
        return accesos_por_rol(db, "admin")
    if u.accesos is not None:
        return {a for a in u.accesos.split(",") if a}
    return accesos_por_rol(db, u.rol)


def puede(db: Session, u: Usuario, clave: str) -> bool:
    return u.es_admin or clave in accesos_de(db, u)


def secciones_de(db: Session, u: Usuario) -> list[Seccion]:
    todas = db.scalars(select(Seccion).order_by(Seccion.orden, Seccion.id)).all()
    acc = accesos_de(db, u)
    return [s for s in todas if s.slug in acc]


def requiere(clave: str):
    """Dependencia: el usuario tiene que tener ese acceso (sección o función)."""
    def dep(u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)) -> Usuario:
        if not puede(db, u, clave):
            raise SinPermiso()
        return u
    return dep


def requiere_alguno(*claves: str):
    """Dependencia: alcanza con tener uno de esos accesos."""
    def dep(u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)) -> Usuario:
        if not any(puede(db, u, c) for c in claves):
            raise SinPermiso()
        return u
    return dep


def requiere_seccion(slug: str):
    return requiere(slug)
