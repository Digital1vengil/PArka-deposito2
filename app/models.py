"""Modelos de datos de PARKA Depósito."""
from datetime import datetime, date
from typing import Optional

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, ahora

ROLES = {
    "admin": "Admin",
    "despacho": "Despacho",
    "deposito": "Depósito",
    "recolector": "Buscador / Recolector",   # entra solo a cada búsqueda nueva de Recolección Ecommerce
    "chofer": "Chofer",                       # solo la app PARKA Reparto (sus hojas de ruta); no ve secciones
}

TIPOS_LOTE = {
    "flex": "Flex",
    "colecta": "Colecta",
    "tiendanube": "Tienda Nube",
    "otro": "Otro",
}

ESTADOS_TAREA = {
    "pendiente": "Pendiente",
    "en_curso": "En curso",
    "hecha": "Hecha",
}


class Usuario(Base):
    __tablename__ = "usuarios"
    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(60), unique=True)
    rol: Mapped[str] = mapped_column(String(20), default="despacho")
    pin_hash: Mapped[str] = mapped_column(String(200))
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    intentos_fallidos: Mapped[int] = mapped_column(Integer, default=0)
    bloqueado_hasta: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ultimo_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    # Accesos personalizados (secciones y funciones separadas por coma). Vacío/None = los del rol.
    accesos: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    @property
    def es_admin(self) -> bool:
        return self.rol == "admin"


class Seccion(Base):
    """Área de trabajo (Despacho, Depósito…). Cada rol ve las secciones que tiene permitidas."""
    __tablename__ = "secciones"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(30), unique=True)
    nombre: Mapped[str] = mapped_column(String(60))
    icono: Mapped[str] = mapped_column(String(10), default="📦")
    descripcion: Mapped[str] = mapped_column(String(200), default="")
    orden: Mapped[int] = mapped_column(Integer, default=0)
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    roles: Mapped[str] = mapped_column(String(200), default="admin")  # lista separada por comas

    def permite(self, rol: str) -> bool:
        return rol == "admin" or rol in [r.strip() for r in self.roles.split(",")]


class Lote(Base):
    """Un archivo de despacho cargado (etiquetas ZPL o planilla) — flex, colecta o Tienda Nube."""
    __tablename__ = "lotes"
    id: Mapped[int] = mapped_column(primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    tipo: Mapped[str] = mapped_column(String(20), default="flex")
    nombre: Mapped[str] = mapped_column(String(200))
    creado_por_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    cerrado: Mapped[bool] = mapped_column(Boolean, default=False)
    drive_ids: Mapped[str] = mapped_column(Text, default="")  # ids de archivos de Drive usados (separados por coma)

    creado_por: Mapped[Optional[Usuario]] = relationship()
    paquetes: Mapped[list["Paquete"]] = relationship(back_populates="lote", cascade="all, delete-orphan")


class Tanda(Base):
    """Un período 'Iniciar despacho → Finalizar despacho' de un operario."""
    __tablename__ = "tandas"
    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"))
    inicio: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    fin: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    usuario: Mapped[Usuario] = relationship()
    paquetes: Mapped[list["Paquete"]] = relationship(back_populates="tanda")


class Paquete(Base):
    __tablename__ = "paquetes"
    id: Mapped[int] = mapped_column(primary_key=True)
    lote_id: Mapped[int] = mapped_column(ForeignKey("lotes.id"), index=True)
    tracking: Mapped[str] = mapped_column(String(60), index=True)
    venta_id: Mapped[str] = mapped_column(String(60), default="")
    comprador: Mapped[str] = mapped_column(String(120), default="")
    producto: Mapped[str] = mapped_column(Text, default="")
    sku: Mapped[str] = mapped_column(String(200), default="")
    cantidad: Mapped[int] = mapped_column(Integer, default=1)
    estado: Mapped[str] = mapped_column(String(20), default="pendiente")
    escaneado_por_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    escaneado_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    codigo_raw: Mapped[str] = mapped_column(String(300), default="")
    tanda_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tandas.id"), nullable=True)
    tracking_alt: Mapped[str] = mapped_column(String(80), default="")  # TN: código de tracking del envío
    drive_enviado: Mapped[bool] = mapped_column(Boolean, default=False)  # ya registrado como despachado en Drive
    drive_pend_fecha: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # día en que se informó como pendiente

    lote: Mapped[Lote] = relationship(back_populates="paquetes")
    escaneado_por: Mapped[Optional[Usuario]] = relationship()
    tanda: Mapped[Optional[Tanda]] = relationship(back_populates="paquetes")


class Tarea(Base):
    __tablename__ = "tareas"
    id: Mapped[int] = mapped_column(primary_key=True)
    seccion_slug: Mapped[str] = mapped_column(String(30), index=True)
    titulo: Mapped[str] = mapped_column(String(200))
    detalle: Mapped[str] = mapped_column(Text, default="")
    origen: Mapped[str] = mapped_column(String(10), default="manual")  # auto | manual
    ref: Mapped[str] = mapped_column(String(60), default="", index=True)  # ej. "lote:12"
    prioridad: Mapped[str] = mapped_column(String(10), default="normal")  # normal | alta
    estado: Mapped[str] = mapped_column(String(20), default="pendiente", index=True)
    asignado_a_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    creado_por_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    creada_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    vence: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    hecha_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    hecha_por_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)

    asignado_a: Mapped[Optional[Usuario]] = relationship(foreign_keys=[asignado_a_id])
    creado_por: Mapped[Optional[Usuario]] = relationship(foreign_keys=[creado_por_id])
    hecha_por: Mapped[Optional[Usuario]] = relationship(foreign_keys=[hecha_por_id])

    def vencida(self, hoy_: date) -> bool:
        return self.estado != "hecha" and self.vence is not None and self.vence < hoy_


class Actividad(Base):
    """Registro de auditoría: quién hizo qué y cuándo."""
    __tablename__ = "actividad"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=ahora, index=True)
    usuario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True, index=True)
    seccion_slug: Mapped[str] = mapped_column(String(30), default="")
    accion: Mapped[str] = mapped_column(String(40))
    detalle: Mapped[str] = mapped_column(Text, default="")

    usuario: Mapped[Optional[Usuario]] = relationship()


class Articulo(Base):
    """Catálogo de artículos: código de barras ↔ SKU. Se sincroniza desde Odoo (o del PDF de etiquetas)."""
    __tablename__ = "articulos"
    id: Mapped[int] = mapped_column(primary_key=True)
    codigo_barras: Mapped[str] = mapped_column(String(20), default="", index=True)
    sku: Mapped[str] = mapped_column(String(80), default="", index=True)
    nombre: Mapped[str] = mapped_column(String(200), default="")
    articulo: Mapped[str] = mapped_column(String(120), default="", index=True)
    variante: Mapped[str] = mapped_column(String(120), default="")
    color: Mapped[str] = mapped_column(String(40), default="")
    talle: Mapped[str] = mapped_column(String(20), default="")
    actualizado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    # Datos que llegan de la sincronización con Odoo (product.product)
    odoo_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    marca: Mapped[str] = mapped_column(String(40), default="")
    color_nombre: Mapped[str] = mapped_column(String(60), default="")
    categoria: Mapped[str] = mapped_column(String(80), default="")
    stock: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    meli_id: Mapped[str] = mapped_column(String(40), default="")
    inactivo: Mapped[bool] = mapped_column(Boolean, default=False)  # archivado en Odoo


class EnvioManual(Base):
    """SmartPost: envíos cargados a mano (como en el scanner). Se anotan en el LogDia del Apps Script."""
    __tablename__ = "envios_manuales"
    id: Mapped[int] = mapped_column(primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    hora: Mapped[str] = mapped_column(String(5), default="")
    plataforma: Mapped[str] = mapped_column(String(2), default="ML")  # ML | TN
    nombre: Mapped[str] = mapped_column(String(120), default="")
    articulo: Mapped[str] = mapped_column(String(200), default="")
    numero: Mapped[str] = mapped_column(String(60), default="")
    usuario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    drive_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    drive_error: Mapped[str] = mapped_column(Text, default="")
    creado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)

    usuario: Mapped[Optional[Usuario]] = relationship()


class Devolucion(Base):
    __tablename__ = "devoluciones"
    id: Mapped[int] = mapped_column(primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    usuario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    plataforma: Mapped[str] = mapped_column(String(8), default="")  # resumen: ML | TN | ML+TN
    drive_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    drive_resp: Mapped[str] = mapped_column(Text, default="")  # respuesta del script (JSON) o error
    # Ruta de devolución: "" (se subió directo) | PENDIENTE (guardando en racks) | DEVUELTO | SIN_DEVOLVER
    ruta_estado: Mapped[str] = mapped_column(String(14), default="")

    usuario: Mapped[Optional[Usuario]] = relationship()
    items: Mapped[list["DevolucionItem"]] = relationship(back_populates="devolucion", cascade="all, delete-orphan")


class DevolucionItem(Base):
    __tablename__ = "devolucion_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    devolucion_id: Mapped[int] = mapped_column(ForeignKey("devoluciones.id"), index=True)
    articulo_id: Mapped[Optional[int]] = mapped_column(ForeignKey("articulos.id"), nullable=True)
    codigo_barras: Mapped[str] = mapped_column(String(20), default="")
    sku: Mapped[str] = mapped_column(String(80), default="")
    articulo: Mapped[str] = mapped_column(String(120), default="")
    color: Mapped[str] = mapped_column(String(60), default="")
    talle: Mapped[str] = mapped_column(String(20), default="")
    estado: Mapped[str] = mapped_column(String(10), default="OPTIMO")  # OPTIMO | FALLA | AJENO (no lo vendemos)
    plataforma: Mapped[str] = mapped_column(String(4), default="")  # ML | TN (por prenda)
    venta: Mapped[str] = mapped_column(String(40), default="")  # nº de venta (ML) o nº de orden (TN)
    llego: Mapped[str] = mapped_column(String(10), default="")  # MISMO | DISTINTO | AJENO (qué llegó vs la venta)
    reemplaza: Mapped[str] = mapped_column(String(200), default="")  # si llegó un artículo distinto: en lugar de qué prenda de la venta
    ubicacion: Mapped[str] = mapped_column(String(80), default="")  # rack donde se guardó (ruta de devolución)
    ubic_odoo: Mapped[str] = mapped_column(String(80), default="")  # ubicación del producto en Odoo (se verifica al guardar)
    guardados: Mapped[str] = mapped_column(Text, default="")  # JSON {rack: unidades} confirmadas en la ruta
    devuelto_cant: Mapped[int] = mapped_column(Integer, default=0)  # unidades ya confirmadas en su rack
    devuelto_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # última confirmación
    cantidad: Mapped[int] = mapped_column(Integer, default=1)

    devolucion: Mapped[Devolucion] = relationship(back_populates="items")


class Conteo(Base):
    __tablename__ = "conteos"
    id: Mapped[int] = mapped_column(primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    usuario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    nota: Mapped[str] = mapped_column(String(200), default="")  # ubicación / referencia opcional
    total: Mapped[int] = mapped_column(Integer, default=0)

    usuario: Mapped[Optional[Usuario]] = relationship()
    items: Mapped[list["ConteoItem"]] = relationship(back_populates="conteo", cascade="all, delete-orphan")


class ConteoItem(Base):
    __tablename__ = "conteo_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    conteo_id: Mapped[int] = mapped_column(ForeignKey("conteos.id"), index=True)
    articulo_id: Mapped[Optional[int]] = mapped_column(ForeignKey("articulos.id"), nullable=True)
    codigo_barras: Mapped[str] = mapped_column(String(20), default="")
    sku: Mapped[str] = mapped_column(String(80), default="")
    nombre: Mapped[str] = mapped_column(String(200), default="")
    articulo: Mapped[str] = mapped_column(String(120), default="")
    color: Mapped[str] = mapped_column(String(60), default="")
    talle: Mapped[str] = mapped_column(String(20), default="")
    marca: Mapped[str] = mapped_column(String(20), default="")
    cantidad: Mapped[int] = mapped_column(Integer, default=1)

    conteo: Mapped[Conteo] = relationship(back_populates="items")


class Cierre(Base):
    __tablename__ = "cierres"
    id: Mapped[int] = mapped_column(primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    enviado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    destinatario: Mapped[str] = mapped_column(String(200), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    automatico: Mapped[bool] = mapped_column(Boolean, default=False)


Index("ix_paquete_estado_lote", Paquete.lote_id, Paquete.estado)


class Ajuste(Base):
    """Configuración guardada desde la app (conexiones a ParkaHub, Gemini). Pisa lo que diga el .env."""
    __tablename__ = "ajustes"
    clave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[str] = mapped_column(Text, default="")
    actualizado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora, onupdate=ahora)


class ChatMensaje(Base):
    """Historial del Asistente (Gemini), uno por usuario."""
    __tablename__ = "chat_mensajes"
    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    rol: Mapped[str] = mapped_column(String(10))  # user | model
    texto: Mapped[str] = mapped_column(Text, default="")
    herramientas: Mapped[str] = mapped_column(Text, default="")  # nombres de las consultas que usó (para mostrar)
    ts: Mapped[datetime] = mapped_column(DateTime, default=ahora, index=True)


# ─────────────── Mapeo del depósito ───────────────
class Ubicacion(Base):
    """Lugar físico del depósito: zona / pasillo / módulo (estantería) / nivel. El código va en la etiqueta."""
    __tablename__ = "ubicaciones"
    id: Mapped[int] = mapped_column(primary_key=True)
    codigo: Mapped[str] = mapped_column(String(30), unique=True, index=True)  # ej. "A-03-2"
    zona: Mapped[str] = mapped_column(String(40), default="")
    pasillo: Mapped[str] = mapped_column(String(10), default="")
    modulo: Mapped[int] = mapped_column(Integer, default=0)
    nivel: Mapped[int] = mapped_column(Integer, default=0)
    orden: Mapped[int] = mapped_column(Integer, default=0, index=True)  # orden de la ruta de recolección
    descripcion: Mapped[str] = mapped_column(String(200), default="")
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)

    asignaciones: Mapped[list["UbicacionArticulo"]] = relationship(back_populates="ubicacion",
                                                                   cascade="all, delete-orphan")


class UbicacionArticulo(Base):
    """Qué hay en cada ubicación: un modelo entero, un color de ese modelo o una variante puntual."""
    __tablename__ = "ubicacion_articulos"
    id: Mapped[int] = mapped_column(primary_key=True)
    ubicacion_id: Mapped[int] = mapped_column(ForeignKey("ubicaciones.id"), index=True)
    modelo: Mapped[str] = mapped_column(String(120), default="", index=True)  # Articulo.articulo
    color: Mapped[str] = mapped_column(String(60), default="")               # vacío = todos los colores
    articulo_id: Mapped[Optional[int]] = mapped_column(ForeignKey("articulos.id"), nullable=True, index=True)
    nota: Mapped[str] = mapped_column(String(120), default="")
    creado_por_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)

    ubicacion: Mapped[Ubicacion] = relationship(back_populates="asignaciones")
    articulo_ref: Mapped[Optional[Articulo]] = relationship()


class SkuVinculo(Base):
    """SKU que viene de ML / Tienda Nube ↔ artículo del catálogo (cuando no coincide solo)."""
    __tablename__ = "sku_vinculos"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku_externo: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    articulo_id: Mapped[int] = mapped_column(ForeignKey("articulos.id"))
    creado_por_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)

    articulo: Mapped[Articulo] = relationship()


# ─────────────── Recolección (picking de las ventas del día) ───────────────
class Recoleccion(Base):
    __tablename__ = "recolecciones"
    id: Mapped[int] = mapped_column(primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    usuario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    nombre: Mapped[str] = mapped_column(String(200), default="")
    origen: Mapped[str] = mapped_column(String(20), default="archivo")  # drive | despacho | archivo
    tipo: Mapped[str] = mapped_column(String(20), default="")          # flex | colecta | tiendanube | mixto
    paquetes: Mapped[int] = mapped_column(Integer, default=0)
    estado: Mapped[str] = mapped_column(String(20), default="abierta")  # abierta | cerrada
    cerrada_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cerrada_por_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)

    usuario: Mapped[Optional[Usuario]] = relationship(foreign_keys=[usuario_id])
    cerrada_por: Mapped[Optional[Usuario]] = relationship(foreign_keys=[cerrada_por_id])
    items: Mapped[list["RecoleccionItem"]] = relationship(back_populates="recoleccion",
                                                          cascade="all, delete-orphan")


class RecoleccionItem(Base):
    __tablename__ = "recoleccion_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    recoleccion_id: Mapped[int] = mapped_column(ForeignKey("recolecciones.id"), index=True)
    sku: Mapped[str] = mapped_column(String(200), default="")       # tal como vino de ML / TN
    producto: Mapped[str] = mapped_column(Text, default="")         # título de la publicación
    articulo_id: Mapped[Optional[int]] = mapped_column(ForeignKey("articulos.id"), nullable=True)
    articulo: Mapped[str] = mapped_column(String(120), default="")
    color: Mapped[str] = mapped_column(String(60), default="")
    talle: Mapped[str] = mapped_column(String(20), default="")
    codigo_barras: Mapped[str] = mapped_column(String(20), default="")
    ubicacion: Mapped[str] = mapped_column(String(120), default="")  # códigos (puede haber más de uno)
    orden_ruta: Mapped[int] = mapped_column(Integer, default=999999)
    cantidad: Mapped[int] = mapped_column(Integer, default=1)
    recogido: Mapped[int] = mapped_column(Integer, default=0)
    ventas: Mapped[str] = mapped_column(Text, default="")            # nº de venta / envío, separados por coma
    recogido_por_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    recogido_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    recoleccion: Mapped[Recoleccion] = relationship(back_populates="items")


# ─────────────── Control de paquetes (verificación al armar cada paquete) ───────────────
class Control(Base):
    """Una tanda de control: los paquetes de un archivo (o varios) de etiquetas / ventas del día."""
    __tablename__ = "controles"
    id: Mapped[int] = mapped_column(primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    usuario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    nombre: Mapped[str] = mapped_column(String(200), default="")
    origen: Mapped[str] = mapped_column(String(20), default="archivo")  # drive | despacho | archivo
    tipo: Mapped[str] = mapped_column(String(20), default="")          # flex | colecta | tiendanube
    archivos: Mapped[str] = mapped_column(Text, default="")            # nombres de los archivos cargados
    estado: Mapped[str] = mapped_column(String(20), default="abierto")  # abierto | cerrado
    cerrado_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    modo: Mapped[str] = mapped_column(String(20), default="")  # "" = control suelto | "ecommerce" = Recolección Ecommerce
    drive_resultado: Mapped[str] = mapped_column(String(300), default="")  # link al Excel guardado en Drive

    usuario: Mapped[Optional[Usuario]] = relationship()
    paquetes: Mapped[list["ControlPaquete"]] = relationship(back_populates="control", cascade="all, delete-orphan",
                                                            order_by="ControlPaquete.id")
    participantes: Mapped[list["ControlParticipante"]] = relationship(back_populates="control",
                                                                      cascade="all, delete-orphan",
                                                                      order_by="ControlParticipante.id")
    boxes: Mapped[list["ControlBox"]] = relationship(back_populates="control", cascade="all, delete-orphan",
                                                     order_by="ControlBox.id")


class ControlPaquete(Base):
    __tablename__ = "control_paquetes"
    id: Mapped[int] = mapped_column(primary_key=True)
    control_id: Mapped[int] = mapped_column(ForeignKey("controles.id"), index=True)
    tracking: Mapped[str] = mapped_column(String(60), index=True)       # nº de envío (lo que se escanea)
    tracking_alt: Mapped[str] = mapped_column(String(80), default="")
    venta_id: Mapped[str] = mapped_column(String(60), default="", index=True)
    comprador: Mapped[str] = mapped_column(String(120), default="")
    tipo: Mapped[str] = mapped_column(String(20), default="")  # flex | colecta | tiendanube | ml
    unidades_etiqueta: Mapped[int] = mapped_column(Integer, default=0)  # lo que dice la etiqueta de ML
    estado: Mapped[str] = mapped_column(String(20), default="pendiente")  # pendiente | en_curso | completo | faltante
    errores: Mapped[int] = mapped_column(Integer, default=0)
    nota: Mapped[str] = mapped_column(String(300), default="")
    etiqueta: Mapped[str] = mapped_column(String(20), default="")   # ML: por_imprimir | lista (etiqueta ya impresa)
    cancelado: Mapped[bool] = mapped_column(Boolean, default=False)   # la venta se canceló: NO despachar
    cancelado_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    creado_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=ahora)  # cuándo entró la venta
    despacho: Mapped[Optional[date]] = mapped_column(Date, nullable=True)   # día en que ML pide despacharla
    limite: Mapped[str] = mapped_column(String(20), default="")             # fecha y hora límite de ML ("15/10 13:00")
    iniciado_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    controlado_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    controlado_por_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)

    control: Mapped[Control] = relationship(back_populates="paquetes")
    controlado_por: Mapped[Optional[Usuario]] = relationship()
    items: Mapped[list["ControlItem"]] = relationship(back_populates="paquete", cascade="all, delete-orphan",
                                                      order_by="ControlItem.id", lazy="selectin")


class ControlItem(Base):
    __tablename__ = "control_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    paquete_id: Mapped[int] = mapped_column(ForeignKey("control_paquetes.id"), index=True)
    sku: Mapped[str] = mapped_column(String(200), default="")         # tal como vino de ML / TN
    producto: Mapped[str] = mapped_column(Text, default="")           # título de la publicación
    variante: Mapped[str] = mapped_column(String(200), default="")    # "Color: Negro · Talle: XL" (de la venta)
    articulo_id: Mapped[Optional[int]] = mapped_column(ForeignKey("articulos.id"), nullable=True)
    aceptados: Mapped[str] = mapped_column(Text, default="")          # ids de artículos válidos (otras marcas)
    articulo: Mapped[str] = mapped_column(String(120), default="")
    color: Mapped[str] = mapped_column(String(60), default="")
    talle: Mapped[str] = mapped_column(String(20), default="")
    codigo_barras: Mapped[str] = mapped_column(String(20), default="")
    ubicacion: Mapped[str] = mapped_column(String(200), default="")
    orden_ruta: Mapped[int] = mapped_column(Integer, default=999999)
    cantidad: Mapped[int] = mapped_column(Integer, default=1)
    escaneado: Mapped[int] = mapped_column(Integer, default=0)
    faltante: Mapped[bool] = mapped_column(Boolean, default=False)
    # Recolección Ecommerce: a quién le toca buscarla y cuántas trajo
    asignado_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True, index=True)
    buscado: Mapped[int] = mapped_column(Integer, default=0)
    buscado_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    buscado_por_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)  # quién la trajo

    paquete: Mapped[ControlPaquete] = relationship(back_populates="items")
    asignado: Mapped[Optional[Usuario]] = relationship(foreign_keys=[asignado_id])
    buscado_por: Mapped[Optional[Usuario]] = relationship(foreign_keys=[buscado_por_id])
    box_items: Mapped[list["ControlBoxItem"]] = relationship(back_populates="item", cascade="all, delete-orphan",
                                                             order_by="ControlBoxItem.id", lazy="selectin")


class ControlParticipante(Base):
    """Recolección Ecommerce: operario que se sumó a buscar las prendas de una tanda."""
    __tablename__ = "control_participantes"
    id: Mapped[int] = mapped_column(primary_key=True)
    control_id: Mapped[int] = mapped_column(ForeignKey("controles.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"))
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    unido_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    salio_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    control: Mapped[Control] = relationship(back_populates="participantes")
    usuario: Mapped[Usuario] = relationship()


class ControlBox(Base):
    """Recolección Ecommerce: caja (BOX) de la mesa donde un operario deja las prendas que trajo.
    Al escanear la etiqueta en la mesa de control se ve en qué BOX está cada venta. El número se libera
    (se puede volver a usar) cuando todas sus ventas quedan controladas."""
    __tablename__ = "control_boxes"
    id: Mapped[int] = mapped_column(primary_key=True)
    control_id: Mapped[int] = mapped_column(ForeignKey("controles.id"), index=True)
    numero: Mapped[int] = mapped_column(Integer, default=1)
    usuario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)  # quién la trajo
    creado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)

    control: Mapped[Control] = relationship(back_populates="boxes")
    usuario: Mapped[Optional[Usuario]] = relationship()
    items: Mapped[list["ControlBoxItem"]] = relationship(back_populates="box", cascade="all, delete-orphan",
                                                         order_by="ControlBoxItem.id", lazy="selectin")


class ControlBoxItem(Base):
    """Cuántas unidades de una línea de venta quedaron en un BOX."""
    __tablename__ = "control_box_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    box_id: Mapped[int] = mapped_column(ForeignKey("control_boxes.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("control_items.id"), index=True)
    cantidad: Mapped[int] = mapped_column(Integer, default=1)

    box: Mapped[ControlBox] = relationship(back_populates="items")
    item: Mapped["ControlItem"] = relationship(back_populates="box_items")


class ControlEvento(Base):
    """Lo que hay que mirar después: prendas equivocadas, sobrantes, faltantes, reinicios."""
    __tablename__ = "control_eventos"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=ahora, index=True)
    control_id: Mapped[int] = mapped_column(ForeignKey("controles.id", ondelete="CASCADE"), index=True)
    paquete_id: Mapped[Optional[int]] = mapped_column(ForeignKey("control_paquetes.id", ondelete="CASCADE"),
                                                      nullable=True)
    usuario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    tipo: Mapped[str] = mapped_column(String(20))   # equivocado | sobra | desconocido | faltante | reinicio
    codigo: Mapped[str] = mapped_column(String(80), default="")
    detalle: Mapped[str] = mapped_column(Text, default="")

    usuario: Mapped[Optional[Usuario]] = relationship()


class TnEnvio(Base):
    """Tienda Nube: órdenes que Despacho marcó como enviadas (DISPATCHED). Si falla se reintenta solo."""
    __tablename__ = "tn_envios"
    id: Mapped[int] = mapped_column(primary_key=True)
    numero: Mapped[str] = mapped_column(String(30), unique=True, index=True)   # nº de orden de TN
    orden_id: Mapped[str] = mapped_column(String(30), default="")
    estado: Mapped[str] = mapped_column(String(20), default="pendiente")       # pendiente | ok | error
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    usuario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    hecho_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class DespachoEscaneo(Base):
    """Despacho en vivo: envío / orden que ya se escaneó (salió) en el día. Lo comparten todos los dispositivos,
    para que la lista del día lo muestre en verde y "Cargar" traiga solo lo nuevo."""
    __tablename__ = "despacho_escaneos"
    __table_args__ = (Index("ix_desp_esc_dia", "fecha", "tipo", "tracking", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    tipo: Mapped[str] = mapped_column(String(20))
    tracking: Mapped[str] = mapped_column(String(40))
    venta: Mapped[str] = mapped_column(String(40), default="")
    comprador: Mapped[str] = mapped_column(String(160), default="")
    usuario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=ahora)


class Novedad(Base):
    """Avisos de Recolección Ecommerce para todo el equipo: entró una venta, se canceló una venta…"""
    __tablename__ = "novedades"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=ahora, index=True)
    tipo: Mapped[str] = mapped_column(String(20), default="info")   # nueva | cancelada | info
    texto: Mapped[str] = mapped_column(String(300), default="")
    detalle: Mapped[str] = mapped_column(Text, default="")
    control_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    clave: Mapped[Optional[str]] = mapped_column(String(80), unique=True, nullable=True)  # para no repetir el mismo aviso
    para: Mapped[str] = mapped_column(String(20), default="")   # "" = todo el equipo · "supervision" = admins y supervisores


class PreguntaML(Base):
    """Respondedor de preguntas de Mercado Libre (Gemini): una fila por pregunta de comprador vista.

    estado: nueva (sin borrador todavía) · borrador (lista para revisar/publicar) · publicada (la mandó esta app)
            · respondida_ml (se respondió por otro lado o la borraron) · descartada · error (falló al publicar)
    decision (de Gemini + reglas): simple = se puede publicar sola en modo Mixto · revisar = la tiene que ver una persona
    """
    __tablename__ = "preguntas_ml"
    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    mla: Mapped[str] = mapped_column(String(30), default="")
    publicacion: Mapped[str] = mapped_column(String(300), default="")
    pregunta: Mapped[str] = mapped_column(Text, default="")
    fecha_pregunta: Mapped[str] = mapped_column(String(40), default="")
    estado: Mapped[str] = mapped_column(String(20), default="nueva", index=True)
    borrador: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(String(10), default="")
    categoria: Mapped[str] = mapped_column(String(30), default="")
    motivo: Mapped[str] = mapped_column(String(300), default="")
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    respuesta: Mapped[str] = mapped_column(Text, default="")
    publicada_por: Mapped[str] = mapped_column(String(60), default="")
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    publicada_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(String(300), default="")
    creada_at: Mapped[datetime] = mapped_column(DateTime, default=ahora, index=True)
    actualizada_at: Mapped[datetime] = mapped_column(DateTime, default=ahora, onupdate=ahora)


class OdooPickImpresion(Base):
    """Recolecciones de Odoo (WH/PICK): cada vez que se imprimieron sus etiquetas. Sin impresión = NUEVO."""
    __tablename__ = "odoo_pick_impresiones"
    id: Mapped[int] = mapped_column(primary_key=True)
    picking_id: Mapped[int] = mapped_column(Integer, index=True)
    nombre: Mapped[str] = mapped_column(String(40), default="")
    usuario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    etiquetas: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(DateTime, default=ahora)


class OdooRotulo(Base):
    """Pedidos de Odoo (Magontex: Recolectar y Órdenes de entrega) que vio la app: cuándo llegaron y si se imprimió
    el rótulo de entrega. Sin impresión = NUEVO."""
    __tablename__ = "odoo_rotulos"
    id: Mapped[int] = mapped_column(primary_key=True)
    picking_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(40), default="")
    detectado_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    impreso_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    impreso_por: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    bultos: Mapped[int] = mapped_column(Integer, default=0)      # bultos del último rótulo impreso
    veces: Mapped[int] = mapped_column(Integer, default=0)


class HojaRuta(Base):
    """Hoja de ruta de un chofer: la arma el depósito con pedidos de Odoo; el chofer la recorre en PARKA Reparto."""
    __tablename__ = "hojas_ruta"
    id: Mapped[int] = mapped_column(primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    chofer_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    estado: Mapped[str] = mapped_column(String(20), default="abierta", index=True)   # abierta / cerrada
    nota: Mapped[str] = mapped_column(String(300), default="")
    creada_por: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    creada_at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    cerrada_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ParadaRuta(Base):
    """Parada de una hoja de ruta: el depósito de un expreso (con todos sus clientes) o un cliente (expreso PROPIO)."""
    __tablename__ = "paradas_ruta"
    id: Mapped[int] = mapped_column(primary_key=True)
    hoja_id: Mapped[int] = mapped_column(ForeignKey("hojas_ruta.id"), index=True)
    orden: Mapped[int] = mapped_column(Integer, default=0)
    tipo: Mapped[str] = mapped_column(String(10), default="expreso")   # expreso / cliente
    ref_id: Mapped[int] = mapped_column(Integer, default=0)            # id del transportista o del cliente en Odoo
    nombre: Mapped[str] = mapped_column(String(160), default="")
    direccion: Mapped[str] = mapped_column(String(250), default="")
    localidad: Mapped[str] = mapped_column(String(160), default="")
    telefono: Mapped[str] = mapped_column(String(80), default="")
    estado: Mapped[str] = mapped_column(String(20), default="pendiente")   # pendiente / entregada / incompleta
    nota: Mapped[str] = mapped_column(String(300), default="")
    cerrada_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class BultoRuta(Base):
    """Un bulto (rótulo "3 de 7") de un pedido dentro de una hoja de ruta. entregado_at = el chofer lo escaneó."""
    __tablename__ = "bultos_ruta"
    id: Mapped[int] = mapped_column(primary_key=True)
    hoja_id: Mapped[int] = mapped_column(ForeignKey("hojas_ruta.id"), index=True)
    parada_id: Mapped[int] = mapped_column(ForeignKey("paradas_ruta.id"), index=True)
    picking_id: Mapped[int] = mapped_column(Integer, index=True)
    picking_nombre: Mapped[str] = mapped_column(String(40), default="")
    origen: Mapped[str] = mapped_column(String(40), default="")
    cliente_id: Mapped[int] = mapped_column(Integer, default=0)
    cliente_nombre: Mapped[str] = mapped_column(String(160), default="")
    cliente_codigo: Mapped[str] = mapped_column(String(20), default="")
    bulto: Mapped[int] = mapped_column(Integer, default=1)
    total: Mapped[int] = mapped_column(Integer, default=1)
    entregado_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    entregado_por: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)


class EntregaDespacho(Base):
    """Bulto entregado por el chofer en un despacho de la app Despachos (S01104 · bulto 3 de 9).
    enviado=False hasta que se le avisa a Despachos (falta la ruta de escritura de su API)."""
    __tablename__ = "entregas_despacho"
    id: Mapped[int] = mapped_column(primary_key=True)
    despacho_id: Mapped[str] = mapped_column(String(40), index=True)
    pedido: Mapped[str] = mapped_column(String(40), index=True)
    cliente: Mapped[str] = mapped_column(String(160), default="")
    bulto: Mapped[int] = mapped_column(Integer, default=1)
    total: Mapped[int] = mapped_column(Integer, default=1)
    chofer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    enviado: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(String(300), default="")


class CierreParadaDespacho(Base):
    """El chofer terminó una parada de un despacho (entregada o incompleta), con su nota."""
    __tablename__ = "cierres_parada_despacho"
    id: Mapped[int] = mapped_column(primary_key=True)
    despacho_id: Mapped[str] = mapped_column(String(40), index=True)
    parada: Mapped[str] = mapped_column(String(20))
    nombre: Mapped[str] = mapped_column(String(160), default="")
    estado: Mapped[str] = mapped_column(String(20), default="entregada")
    nota: Mapped[str] = mapped_column(String(300), default="")
    chofer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    enviado: Mapped[bool] = mapped_column(Boolean, default=False)
