"""Mercado Libre directo, a través del proxy de la app de Costos (magontex-costos.pages.dev).

La app de Costos tiene el token de la cuenta PARKA.AR y aplica un perfil de permisos por clave.
PARKA Depósito usa la clave del perfil "publicaciones" (la pasa Gonzalo) y se carga A MANO en
Conexiones → "Mercado Libre directo". Nunca va en el código.

  GET  {base}/api?path=<ruta ML url-encoded>   → {"status": <código ML>, "data": <respuesta ML>}
  POST {base}/write {method, path, body}       → {"status", "ok", "data"}
  GET  {base}/perfil                           → rutas permitidas / bloqueadas de la clave
Header en todas: x-api-key. Sin clave o revocada → 401. Fuera del perfil → 403 con el motivo.

Reglas: una llamada por consulta y con pausa (el token se comparte con Costos y las alertas);
las escrituras impactan en ML de verdad.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import ajustes

TIMEOUT = 20
PAUSA = 0.35          # segundos mínimos entre llamadas al proxy
_lock = threading.Lock()
_ultima = [0.0]
_yo: dict = {}        # caché de /users/me (id del vendedor)


class MeliError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _cfg() -> tuple[str, str]:
    base = (ajustes.leer("meli_url") or "").strip().rstrip("/")
    clave = (ajustes.leer("meli_api_key") or "").strip()
    if not clave:
        raise MeliError("Falta la clave de la API de Mercado Libre (Conexiones → Mercado Libre directo).")
    return base, clave


def configurado() -> bool:
    return bool((ajustes.leer("meli_api_key") or "").strip())


def _esperar_turno():
    with _lock:
        falta = PAUSA - (time.monotonic() - _ultima[0])
        if falta > 0:
            time.sleep(falta)
        _ultima[0] = time.monotonic()


def _pedir(req: urllib.request.Request) -> dict:
    _esperar_turno()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            txt = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(cuerpo)
        except ValueError:
            j = {}
        motivo = (j.get("error") or j.get("message") or j.get("motivo") or "") if isinstance(j, dict) else ""
        if e.code == 401:
            raise MeliError("La clave de Mercado Libre no es válida o fue revocada (pedile una nueva a Gonzalo).", 401) from None
        if e.code == 403:
            raise MeliError("Fuera del perfil de la clave" + (f": {motivo}" if motivo else "")
                            + ". Si hace falta, pedile a Gonzalo que agregue la ruta.", 403) from None
        if isinstance(j, dict) and "status" in j and "data" in j:
            return j  # el proxy respondió con el error de ML adentro
        raise MeliError(f"El proxy de ML respondió error {e.code}" + (f": {motivo}" if motivo else "") + ".", e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise MeliError(f"No se pudo conectar con la API de ML de Costos ({getattr(e, 'reason', e)}).") from None
    try:
        return json.loads(txt)
    except ValueError:
        raise MeliError("El proxy de ML devolvió algo que no es JSON.") from None


def _headers(clave: str, json_body: bool = False) -> dict:
    h = {"x-api-key": clave, "Accept": "application/json", "User-Agent": "PARKA-Deposito/1.0"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _chequear(j: dict, path: str):
    st = j.get("status")
    if isinstance(st, int) and st >= 400:
        d = j.get("data")
        msg = (d.get("message") or d.get("error")) if isinstance(d, dict) else ""
        if st == 404:
            raise MeliError("Mercado Libre no lo encontró (404).", 404)
        raise MeliError(f"Mercado Libre respondió {st} en {path.split('?')[0]}" + (f": {msg}" if msg else "") + ".", st)


def get(path: str, api_version: int | None = None) -> dict | list:
    """GET a ML. `path` es la ruta de ML con sus query params, ej. '/items/MLA123' o '/orders/search?seller=1&limit=50'."""
    if not path.startswith("/"):
        path = "/" + path
    base, clave = _cfg()
    qs = urllib.parse.urlencode({"path": path, **({"api_version": api_version} if api_version else {})})
    j = _pedir(urllib.request.Request(f"{base}/api?{qs}", headers=_headers(clave)))
    if not (isinstance(j, dict) and "status" in j and ("data" in j or "body" in j)):
        return j  # el proxy devolvió la respuesta de ML tal cual (sin envoltorio)
    _chequear(j, path)
    return j["data"] if "data" in j else j.get("body")


def crudo(path: str, api_version: int | None = None):
    """Para diagnóstico (solo admin): lo que devuelve el proxy, sin tocar."""
    if not path.startswith("/"):
        path = "/" + path
    base, clave = _cfg()
    qs = urllib.parse.urlencode({"path": path, **({"api_version": api_version} if api_version else {})})
    return _pedir(urllib.request.Request(f"{base}/api?{qs}", headers=_headers(clave)))


def write(method: str, path: str, body: dict | None = None) -> dict:
    """POST/PUT/DELETE a ML. Impacta de verdad: usar solo desde acciones confirmadas en pantalla."""
    method = method.upper()
    if method not in ("POST", "PUT", "DELETE"):
        raise MeliError("método inválido")
    base, clave = _cfg()
    datos = json.dumps({"method": method, "path": path, "body": body or {}}).encode("utf-8")
    j = _pedir(urllib.request.Request(f"{base}/write", data=datos, method="POST", headers=_headers(clave, True)))
    _chequear(j, path)
    return j


def perfil() -> dict:
    base, clave = _cfg()
    return _pedir(urllib.request.Request(f"{base}/perfil", headers=_headers(clave)))


def yo() -> dict:
    if not _yo:
        d = get("/users/me")
        _yo.update(d if isinstance(d, dict) else {})
    return _yo


def seller_id() -> int:
    """Id del vendedor: /users/me; si el proxy no lo da, el cargado a mano en Conexiones;
    si tampoco, se saca de las preguntas recibidas (cada pregunta trae seller_id)."""
    sid = None
    try:
        sid = yo().get("id")
    except MeliError:
        pass
    if not sid:
        manual = (ajustes.leer("meli_seller_id") or "").strip()
        if manual.isdigit():
            sid = int(manual)
    if not sid:
        try:
            d = get("/my/received_questions/search?limit=1") or {}
            qs = (d.get("questions") or d.get("results") or []) if isinstance(d, dict) else []
            sid = (qs[0] or {}).get("seller_id") if qs else None
        except MeliError:
            sid = None
        if sid:
            _yo["id"] = sid
    if not sid:
        raise MeliError("No se pudo leer el id del vendedor de ML: cargalo a mano en Conexiones → Mercado Libre directo "
                        "(\"Id del vendedor\").")
    return int(sid)


def olvidar_cache():
    _yo.clear()


def probar() -> dict:
    """Para el botón 'Probar conexión': quién es la cuenta y qué perfil tiene la clave."""
    olvidar_cache()
    u = dict(yo())
    if not u.get("id"):
        try:
            u["id"] = seller_id()
        except MeliError:
            pass
    p = {}
    try:
        p = perfil()
    except MeliError:
        pass
    nombre = p.get("perfil") or p.get("nombre") or p.get("name") or "" if isinstance(p, dict) else ""
    return {"ok": True, "cuenta": u.get("nickname"), "id": u.get("id"), "perfil": nombre}
