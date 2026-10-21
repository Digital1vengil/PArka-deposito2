"""PARKA Depósito — app de operaciones por secciones.

Arrancar en desarrollo:   uvicorn app.main:app --reload
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.gzip import GZipMiddleware

from . import config
from .auth import NoAutenticado, SinPermiso
from .db import Base, engine, SessionLocal, migrar
from .routers import auth as r_auth, inicio, despacho, tareas, supervision, admin, articulos, smartpost, devoluciones, conteo, asistente, mercadolibre, mapeo, recoleccion, control, catalogo, ecommerce, etiquetas, tiendanube, cambios, recolecciones_odoo, importar, rotulos, hojas_ruta
from .seed import sembrar
from .templating import render

log = logging.getLogger("parka")
STATIC = Path(__file__).parent / "static"


def _iniciar_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from .db import hoy, ahora
    # coalesce: si una vuelta se atrasó no se ejecutan las acumuladas; max_instances=1: nunca dos a la vez
    sch = BackgroundScheduler(timezone=config.TZ,
                              job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60})

    # 1) Cierre propio por mail SMTP (opcional, apagado por defecto)
    if config.CIERRE_AUTOMATICO:
        from .services.cierre import enviar_cierre
        hh, mm = (config.CIERRE_HORA.split(":") + ["0"])[:2]

        def job_smtp():
            db = SessionLocal()
            try:
                r = enviar_cierre(db, hoy(), automatico=True)
                log.info("Cierre SMTP %s: %s", hoy(), "OK" if r.ok else r.error)
            finally:
                db.close()
        sch.add_job(job_smtp, "cron", day_of_week=config.CIERRE_DIAS, hour=int(hh), minute=int(mm), id="cierre")

    # 2) Google: unos minutos antes del cierre automático del Apps Script se informan
    #    los despachados que falten y los pendientes del día, para que el mail salga completo.
    if config.GAS_AUTO and config.GAS_URL:
        from .services import drive
        estado = {"hora": None, "leida": None, "informado": None}

        def job_google():
            now = ahora()
            if estado["hora"] is None or estado["leida"] is None or (now - estado["leida"]).total_seconds() > 3600:
                cfg = drive.leer_config()
                estado["hora"] = int(cfg.get("hora", estado["hora"] or 17))
                estado["leida"] = now
            limite = now.replace(hour=estado["hora"], minute=0, second=0) \
                - __import__("datetime").timedelta(minutes=config.GAS_MINUTOS_ANTES)
            if now >= limite and now.hour < estado["hora"] and estado["informado"] != hoy():
                db = SessionLocal()
                try:
                    r = drive.informar_dia(db)
                    estado["informado"] = hoy()
                    log.info("Informado a Google antes del cierre: %s", r)
                except Exception as e:  # noqa: BLE001
                    log.warning("No se pudo informar a Google: %s", e)
                finally:
                    db.close()
        sch.add_job(job_google, "interval", minutes=2, id="google")

    # 2b) DEPOSITO PARKA en Drive: cada 15 min se reemplazan los Excel del día que cambiaron
    #     (Despachos / Devoluciones / Conteos / Cierre). Pasada la medianoche se cierra también el día anterior.
    def job_deposito_drive():
        from .services import deposito_drive
        db = SessionLocal()
        try:
            if not (deposito_drive.configurado(db) and deposito_drive.automatico(db)):
                return
            now = ahora()
            fechas = [hoy()] + ([hoy() - __import__("datetime").timedelta(days=1)] if now.hour == 0 else [])
            for f in fechas:
                r = deposito_drive.sincronizar(db, f)
                if r["archivos"]:
                    log.info("DEPOSITO PARKA %s: %s", f, [a["nombre"] for a in r["archivos"]])
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo guardar en DEPOSITO PARKA: %s", e)
        finally:
            db.close()
    sch.add_job(job_deposito_drive, "interval", minutes=15, id="deposito_drive")

    # 2c) Mail del cierre a gerencia (por defecto lunes a viernes 17:00) con el Excel del Cierre adjunto.
    #     Días y hora se leen al arrancar (Conexiones → cierre_mail_dias / cierre_mail_hora).
    def job_cierre_mail():
        from .services import ajustes, deposito_drive
        db = SessionLocal()
        try:
            if ajustes.leer("cierre_mail_auto", db).strip() == "0" or deposito_drive.cierre_mail_ya_enviado(db, hoy()):
                return
            r = deposito_drive.enviar_cierre_mail(db, hoy(), automatico=True)
            log.info("Cierre por mail %s → %s: %s", hoy(), r.destinatario, "OK" if r.ok else r.error)
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo mandar el cierre por mail: %s", e)
        finally:
            db.close()
    try:
        from .services import ajustes as _aj
        hh, mm = (_aj.leer("cierre_mail_hora").strip().split(":") + ["0"])[:2]
        sch.add_job(job_cierre_mail, "cron", day_of_week=_aj.leer("cierre_mail_dias").strip() or "mon-fri",
                    hour=int(hh), minute=int(mm), id="cierre_mail", misfire_grace_time=1800)
    except Exception as e:  # noqa: BLE001 — un horario mal cargado no tiene que tirar la app
        log.warning("Horario del cierre por mail inválido: %s", e)

    # 3) Odoo: sincroniza el catálogo de artículos cada N minutos (Conexiones → Odoo). Sin datos de Odoo no hace nada.
    def job_odoo():
        from .services import catalogo, odoo
        db = SessionLocal()
        try:
            if catalogo.toca_sincronizar(db):
                r = catalogo.sincronizar(db, "automático")
                log.info("Catálogo sincronizado con Odoo: %s", r)
        except odoo.OdooError as e:
            log.warning("No se pudo sincronizar con Odoo: %s", e)
        finally:
            db.close()
    sch.add_job(job_odoo, "interval", minutes=5, id="odoo", next_run_time=(ahora() + __import__("datetime").timedelta(seconds=30)).replace(tzinfo=config.TZ))

    # 3b) Rótulos: anota la fecha de llegada de los pedidos nuevos de Magontex (Recolectar / Órdenes de entrega)
    def job_rotulos():
        from .services import odoo, rotulos
        db = SessionLocal()
        try:
            if odoo.configurado(db):
                rotulos.revisar_nuevos(db)
        except odoo.OdooError as e:
            log.warning("Rótulos: no se pudo leer Odoo: %s", e)
        finally:
            db.close()
    sch.add_job(job_rotulos, "interval", minutes=2, id="rotulos")

    # 4) Tienda Nube: reintenta marcar como enviadas las órdenes que fallaron al despachar
    def job_tn():
        from .services import tiendanube as tn
        db = SessionLocal()
        try:
            n = tn.reintentar(db)
            if n:
                log.info("Tienda Nube: %s órdenes marcadas como enviadas en el reintento", n)
        except Exception as e:  # noqa: BLE001
            log.warning("Tienda Nube: reintento falló: %s", e)
        finally:
            db.close()
    sch.add_job(job_tn, "interval", minutes=5, id="tiendanube")

    # 5) Recolección Ecommerce automática: ventas nuevas de ML + TN a la búsqueda del día y avisos de cancelaciones
    def job_ventas():
        from .services import ventas_auto
        db = SessionLocal()
        try:
            if ventas_auto.toca(db):
                r = ventas_auto.sincronizar(db)
                if r.get("nuevas") or r.get("canceladas"):
                    log.info("Ventas automáticas: %s", r)
        except Exception as e:  # noqa: BLE001
            log.warning("Ventas automáticas: falló la revisión: %s", e)
        finally:
            db.close()
    sch.add_job(job_ventas, "interval", minutes=1, id="ventas_auto",
                next_run_time=(ahora() + __import__("datetime").timedelta(seconds=45)).replace(tzinfo=config.TZ))

    # 6) Respondedor de preguntas de ML (Gemini): borradores nuevos y, en modo Mixto / Automático, publica
    def job_respondedor():
        from .services import respondedor
        db = SessionLocal()
        try:
            if respondedor.toca(db):
                r = respondedor.sincronizar(db)
                if r.get("nuevas") or r.get("publicadas"):
                    log.info("Respondedor ML: %s", r)
        except Exception as e:  # noqa: BLE001
            log.warning("Respondedor ML: falló la revisión: %s", e)
        finally:
            db.close()
    sch.add_job(job_respondedor, "interval", minutes=1, id="respondedor",
                next_run_time=(ahora() + __import__("datetime").timedelta(seconds=60)).replace(tzinfo=config.TZ))

    if not sch.get_jobs():
        return None
    sch.start()
    return sch


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    migrar()
    db = SessionLocal()
    try:
        sembrar(db)
        try:
            from .services import ventas_auto
            ventas_auto.limpiar_avisos_cancelada_sueltos(db)
            n = ventas_auto.reparar_arrastre(db)
            if n:
                log.info("Recolección Ecommerce: %s venta(s) de días anteriores volvieron a su búsqueda", n)
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudieron limpiar los avisos de canceladas: %s", e)
    finally:
        db.close()
    sch = _iniciar_scheduler()
    yield
    if sch:
        sch.shutdown(wait=False)


app = FastAPI(title="PARKA Depósito", lifespan=lifespan)
@app.middleware("http")
async def _celulares_a_https(request: Request, call_next):
    """Si un celular entra por http (sin cámara), lo pasa solo a la dirección https."""
    import os
    puerto = os.environ.get("PARKA_HTTPS_PUERTO")
    cliente = request.client.host if request.client else ""
    if (puerto and request.url.scheme == "http" and cliente not in ("127.0.0.1", "::1", "localhost")
            and not request.url.path.startswith(("/salud", "/interno"))):
        host = (request.headers.get("host") or "").split(":")[0]
        destino = f"https://{host}:{puerto}{request.url.path}"
        if request.url.query:
            destino += "?" + request.url.query
        return RedirectResponse(destino, status_code=307)
    return await call_next(request)


@app.middleware("http")
async def _cache_estaticos(request: Request, call_next):
    """Los archivos de /static con ?v=<versión> no cambian nunca: el navegador los guarda un año y no
    vuelve a preguntar en cada pantalla. Sin ?v= se revalidan siempre (no-cache)."""
    resp = await call_next(request)
    p = request.url.path
    if p.startswith(("/static/", "/etiquetas-fonts/")) and resp.status_code == 200:
        resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if request.query_params.get("v")
                                         else "public, max-age=0, must-revalidate")
    return resp


app.add_middleware(GZipMiddleware, minimum_size=1500, compresslevel=5)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, session_cookie="parka_dep",
                   max_age=config.SESION_HORAS * 3600, same_site="lax")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
app.mount("/etiquetas-fonts", StaticFiles(directory=str(Path(__file__).parent / "fonts")), name="etiquetas-fonts")

for r in (r_auth.router, inicio.router, despacho.router, tareas.router, supervision.router, admin.router,
          articulos.router, smartpost.router, devoluciones.router, conteo.router,
          asistente.router, mercadolibre.router, mapeo.router, recoleccion.router, control.router,
          catalogo.router, ecommerce.router, etiquetas.router, tiendanube.router, cambios.router, recolecciones_odoo.router, importar.router, rotulos.router, hojas_ruta.router):
    app.include_router(r)


def _quiere_json(request: Request) -> bool:
    return "/api/" in request.url.path or "application/json" in request.headers.get("accept", "") \
        or request.headers.get("x-requested-with") == "fetch"


@app.exception_handler(NoAutenticado)
async def _no_auth(request: Request, exc: NoAutenticado):
    if _quiere_json(request):
        return JSONResponse({"error": "Sesión vencida, volvé a ingresar"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(SinPermiso)
async def _sin_permiso(request: Request, exc: SinPermiso):
    if _quiere_json(request):
        return JSONResponse({"error": "No tenés permiso para esto"}, status_code=403)
    return render(request, "error.html", titulo="Sin permiso",
                  mensaje="Tu rol no tiene acceso a esta sección.", status_code=403)


# PWA: el service worker y el manifest tienen que servirse desde la raíz
@app.get("/sw.js", include_in_schema=False)
def sw():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript")


@app.get("/manifest.json", include_in_schema=False)
def manifest():
    return FileResponse(STATIC / "manifest.json", media_type="application/manifest+json")


VERSION = (Path(__file__).parent / "VERSION").read_text(encoding="utf-8").strip() \
    if (Path(__file__).parent / "VERSION").exists() else "dev"


@app.get("/celular", include_in_schema=False)
def celular(request: Request):
    """Página con el QR para abrir la app en el celular (con cámara)."""
    import os
    from .certificado import ip_principal, qr_svg
    puerto = os.environ.get("PARKA_HTTPS_PUERTO")
    url = os.environ.get("PARKA_URL_CELULAR") or (f"https://{ip_principal()}:{puerto}" if puerto else
                                                    f"https://{request.headers.get('host', '')}" if os.environ.get("PARKA_NUBE") else "")
    destino = request.query_params.get("ir", "/despacho")
    return render(request, "celular.html", url=url, url_ir=(url + destino) if url else "",
                  qr=qr_svg(url + destino) if url else "", u=None)


@app.get("/salud", include_in_schema=False)
def salud():
    return {"ok": True, "app": "parka-deposito", "version": VERSION}


@app.post("/interno/apagar", include_in_schema=False)
def apagar(request: Request):
    """Lo usa el lanzador para cerrar una versión vieja que quedó abierta. Solo desde esta misma PC."""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        return JSONResponse({"error": "solo local"}, status_code=403)
    import os
    import threading
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True}
