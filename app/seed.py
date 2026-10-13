"""Datos iniciales: admin y secciones base."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import hash_pin
from .config import ADMIN_NOMBRE, ADMIN_PIN
from .models import Seccion, Usuario

SECCIONES_BASE = [
    dict(slug="despacho", nombre="Despacho", icono="🚚", orden=1, activa=True,
         roles="admin,despacho,deposito",
         descripcion="Flex, colecta y Tienda Nube: cargá el lote y escaneá los paquetes"),
    dict(slug="conteo", nombre="Conteo", icono="🔢", orden=3, activa=True,
         roles="admin,deposito",
         descripcion="Elegí el artículo, cargá la cantidad de cada color y talle, y descargá el Excel del conteo"),
    dict(slug="tareas", nombre="Tareas", icono="✅", orden=7, activa=True,
         roles="admin,despacho,deposito",
         descripcion="Tareas de todas las áreas: asignadas, pendientes y vencidas"),
    dict(slug="articulos", nombre="Artículos", icono="🏷️", orden=8, activa=True,
         roles="admin,despacho,deposito",
         descripcion="Buscá por código de barras, SKU o nombre"),
    dict(slug="asistente", nombre="ParkIA", icono="P", orden=10, activa=True, roles="admin",
         descripcion="Preguntale por ventas, publicaciones, preguntas de ML, devoluciones, envíos y tareas (Gemini)"),
    dict(slug="ml", nombre="Mercado Libre", icono="🛒", orden=11, activa=True, roles="admin",
         descripcion="Preguntas con respondedor automático (Gemini), ventas, devoluciones y reputación"),
    dict(slug="ecommerce", nombre="Recolección Ecommerce", icono="🛍️", orden=4, activa=True,
         roles="admin,despacho,deposito",
         descripcion="Ventas del día de ML y Tienda Nube: sumate a la búsqueda, juntá tu tramo y controlá cada paquete"),
    dict(slug="cambios", nombre="Cambios", icono="🔁", orden=5, activa=True,
         roles="admin,despacho,deposito",
         descripcion="Cambios aceptados en la app de cambios: qué mandarle a cada cliente, por dónde, y marcar el despacho"),
    dict(slug="mapeo", nombre="Mapeo del depósito", icono="🗺️", orden=6, activa=True,
         roles="admin,deposito",
         descripcion="Ubicaciones (pasillo, módulo, nivel), qué hay en cada una, mapa y etiquetas"),
    dict(slug="devoluciones", nombre="Devoluciones", icono="↩️", orden=2, activa=True,
         roles="admin,deposito",
         descripcion="Buscá el artículo, elegí color, talle y cantidad, y confirmá: queda en Drive y salen las etiquetas"),
    dict(slug="pick_odoo", nombre="Recolecciones Odoo", icono="📋", orden=9, activa=True,
         roles="admin,deposito",
         descripcion="Recolecciones abiertas de Odoo (WH/PICK) con aviso de las nuevas, y sus etiquetas en rollo Honeywell"),
]


def cargar_catalogo_inicial(db: Session) -> None:
    """Si no hay artículos, carga el catálogo incluido (app/data/articulos.csv)."""
    import csv
    from pathlib import Path
    from .models import Articulo
    if db.scalar(select(Articulo.id).limit(1)):
        return
    f = Path(__file__).parent / "data" / "articulos.csv"
    if not f.exists():
        return
    with f.open(encoding="utf-8") as fh:
        filas = list(csv.DictReader(fh))
    db.bulk_insert_mappings(Articulo, [{k: (r.get(k) or "") for k in
                                        ("codigo_barras", "sku", "nombre", "articulo", "variante", "color", "talle")}
                                       for r in filas])
    db.commit()


def sembrar(db: Session) -> None:
    # Versiones anteriores: la sección "Depósito / stock" pasa a ser "Devoluciones"
    vieja = db.scalar(select(Seccion).where(Seccion.slug == "deposito"))
    if vieja and not db.scalar(select(Seccion).where(Seccion.slug == "devoluciones")):
        vieja.slug, vieja.nombre, vieja.icono, vieja.activa, vieja.orden = "devoluciones", "Devoluciones", "↩️", True, 2
        vieja.descripcion = next(d["descripcion"] for d in SECCIONES_BASE if d["slug"] == "devoluciones")
        for us in db.scalars(select(Usuario).where(Usuario.accesos.is_not(None))).all():
            us.accesos = ",".join("devoluciones" if a == "deposito" else a for a in us.accesos.split(","))
        db.flush()
    for datos in SECCIONES_BASE:
        if not db.scalar(select(Seccion).where(Seccion.slug == datos["slug"])):
            db.add(Seccion(**datos))
    # Recolección y Control de paquetes se unieron en "Recolección Ecommerce" (v2026.10.06-2)
    viejas = {"recoleccion", "control"}
    for us in db.scalars(select(Usuario).where(Usuario.accesos.is_not(None))).all():
        acc = [a for a in us.accesos.split(",") if a]
        if viejas & set(acc):
            nuevos = [a for a in acc if a not in viejas]
            if "ecommerce" not in nuevos:
                nuevos.append("ecommerce")
            us.accesos = ",".join(nuevos)
    from .models import Tarea
    for t in db.scalars(select(Tarea).where(Tarea.seccion_slug.in_(viejas))).all():
        t.seccion_slug = "ecommerce"
    for vieja in db.scalars(select(Seccion).where(Seccion.slug.in_(viejas))).all():
        db.delete(vieja)
    # SmartPost ya está dentro de Despacho (scanner): se quita la sección aparte.
    # Panel PARKA (la otra app, puerto 8000) se quitó en v2026.10.07-8: lo del agente vive en Mercado Libre / Asistente.
    for vieja in db.scalars(select(Seccion).where(Seccion.slug.in_(("smartpost", "panel")))).all():
        db.delete(vieja)
    for us in db.scalars(select(Usuario).where(Usuario.accesos.is_not(None))).all():
        if "panel" in us.accesos.split(","):
            us.accesos = ",".join(a for a in us.accesos.split(",") if a and a != "panel")
    # v2026.10.07-14: el Asistente pasa a ser "ParkIA" (botón flotante en todas las pantallas)
    asis = db.scalar(select(Seccion).where(Seccion.slug == "asistente"))
    if asis and asis.nombre == "Asistente":
        asis.nombre, asis.icono = "ParkIA", "P"
    ml = db.scalar(select(Seccion).where(Seccion.slug == "ml"))
    if ml and "solo lectura" in (ml.descripcion or ""):
        ml.descripcion = next(d["descripcion"] for d in SECCIONES_BASE if d["slug"] == "ml")
    # v2026.10.08-2: rol "Buscador / Recolector" — por defecto ve lo mismo que Depósito + Recolección Ecommerce
    secs = list(db.scalars(select(Seccion)).all())
    if not any("recolector" in (s.roles or "").split(",") for s in secs):
        for s in secs:
            rs = [r.strip() for r in (s.roles or "").split(",") if r.strip()]
            if "deposito" in rs or s.slug == "ecommerce":
                s.roles = ",".join(rs + ["recolector"])
    # v2026.10.08-5: Recolecciones Odoo — el rol Recolector la ve igual que Depósito
    pick = next((s for s in secs if s.slug == "pick_odoo"), None)
    if pick and "recolector" not in (pick.roles or "").split(","):
        pick.roles = ",".join([r for r in (pick.roles or "").split(",") if r] + ["recolector"])
    # El orden de las secciones lo maneja el admin (Secciones → arrastrar). Ya no se reordena solo al arrancar.
    if not db.scalar(select(Usuario).where(Usuario.rol == "admin")):
        db.add(Usuario(nombre=ADMIN_NOMBRE, rol="admin", pin_hash=hash_pin(ADMIN_PIN)))
    db.commit()
    cargar_catalogo_inicial(db)
