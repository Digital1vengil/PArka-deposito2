"""Plano del depósito MAGONTEX (nave de 60 x 10,40 m) y camino entre racks.

Una sola fuente para el visor 3D de Mapeo y la ruta de Devoluciones (salen de las medidas que cargó Diego).
En metros: x va de norte (0) a sur (60) y z de oeste (0, la pared de las persianas) a este (10,40).
Cada rack tiene columnas C01… a lo largo y 3 niveles (N0 = planta baja, N1, N2): el módulo es A01-C03-N1.

Para afuera todo va en "puntos" del plano (ESCALA puntos por metro; 0,0 = esquina norte-este, el oeste queda
abajo, como se dibuja en pantalla). El camino se calcula caminando por los pasillos, no en línea recta.
"""
from __future__ import annotations

import re
from collections import deque

NAVE_L, NAVE_W = 60.0, 10.40
ESCALA = 20                   # puntos por metro → el plano mide 1200 x 208
ANCHO, ALTO = int(NAVE_L * ESCALA), int(NAVE_W * ESCALA)
CELDA_M = 0.25                # grilla para caminar
SEP_M = 0.6                   # del frente del rack al punto del pasillo desde donde se carga
NIVELES = 3

# c: código · v: nombre viejo · z: zona · d: sentido (x = a lo largo de la nave, z = transversal)
# a0: dónde arranca en su sentido · c0: dónde arranca en el otro · len / prof: largo y fondo (m) · n: columnas
# fr: frente (cm) · al: alto de cada nivel (cm) · lug: dónde está · acc: lado del pasillo desde donde se carga
RACKS: list[dict] = [
    {"c": "A01", "v": "Rack 9", "z": "A", "d": "x", "a0": 0, "c0": 0, "len": 16.03, "prof": 1.00, "n": 7, "fr": 229, "al": [163, 171, 171], "lug": "Pared oeste", "acc": "este"},
    {"c": "A02", "v": "Rack 10", "z": "A", "d": "x", "a0": 2.63, "c0": 3.74, "len": 13.86, "prof": 1.35, "n": 6, "fr": 231, "al": [162, 174, 174], "lug": "Batería, cara oeste", "acc": "oeste"},
    {"c": "A03", "v": "Rack 11", "z": "A", "d": "x", "a0": 2.63, "c0": 5.09, "len": 13.92, "prof": 1.35, "n": 6, "fr": 232, "al": [164, 173, 173], "lug": "Batería, cara este", "acc": "este"},
    {"c": "A04", "v": "Rack 12", "z": "A", "d": "x", "a0": 0, "c0": 9.40, "len": 20.61, "prof": 1.00, "n": 9, "fr": 229, "al": [162, 173, 173], "lug": "Pared este", "acc": "oeste"},
    {"c": "A05", "v": "Rack 13", "z": "A", "d": "z", "a0": 2.25, "c0": 0, "len": 4.76, "prof": 1.00, "n": 2, "fr": 238, "al": [174, 176, 176], "lug": "Pared norte", "acc": "sur"},
    {"c": "B01", "v": "Rack 7", "z": "B", "d": "x", "a0": 22.28, "c0": 0, "len": 15.20, "prof": 1.00, "n": 8, "fr": 190, "al": [172, 177, 177], "lug": "Pared oeste", "acc": "este"},
    {"c": "B02", "v": "Rack 8", "z": "B", "d": "x", "a0": 22.11, "c0": 3.81, "len": 15.96, "prof": 1.33, "n": 7, "fr": 228, "al": [175, 178, 178], "lug": "Batería, cara oeste", "acc": "oeste"},
    {"c": "B03", "v": "s/n junto al 8", "z": "B", "d": "x", "a0": 22.11, "c0": 5.14, "len": 16.10, "prof": 1.33, "n": 7, "fr": 230, "al": [180, 180, 180], "lug": "Batería, cara este", "acc": "este"},
    {"c": "B04", "v": "Rack 6", "z": "B", "d": "x", "a0": 30.43, "c0": 9.40, "len": 6.90, "prof": 1.00, "n": 3, "fr": 230, "al": [164, 173, 173], "lug": "Pared este", "acc": "oeste"},
    {"c": "B05", "v": "Rack 6B", "z": "B", "d": "x", "a0": 39.15, "c0": 9.40, "len": 4.60, "prof": 1.00, "n": 2, "fr": 230, "al": [164, 172, 172], "lug": "Pared este", "acc": "oeste"},
    {"c": "B06", "v": "s/n transversal", "z": "B", "d": "z", "a0": 3.50, "c0": 21.11, "len": 6.90, "prof": 1.00, "n": 3, "fr": 230, "al": [195, 146, 146], "lug": "Transversal", "acc": "norte"},
    {"c": "C01", "v": "Rack 1", "z": "C", "d": "x", "a0": 47.23, "c0": 0, "len": 6.90, "prof": 1.00, "n": 3, "fr": 230, "al": [164, 175, 175], "lug": "Pared oeste", "acc": "este"},
    {"c": "C02", "v": "Rack 2", "z": "C", "d": "x", "a0": 45.07, "c0": 4.08, "len": 6.90, "prof": 1.30, "n": 3, "fr": 230, "al": [164, 175, 175], "lug": "Batería, cara oeste", "acc": "oeste"},
    {"c": "C03", "v": "Rack 4", "z": "C", "d": "x", "a0": 45.07, "c0": 5.38, "len": 6.90, "prof": 1.30, "n": 3, "fr": 230, "al": [164, 175, 175], "lug": "Batería, cara este", "acc": "este"},
    {"c": "C04", "v": "Rack 5", "z": "C", "d": "x", "a0": 48.40, "c0": 9.40, "len": 6.90, "prof": 1.00, "n": 3, "fr": 230, "al": [164, 175, 175], "lug": "Pared este", "acc": "oeste"},
    {"c": "C05", "v": "Rack 3", "z": "C", "d": "z", "a0": 0, "c0": 59.00, "len": 9.36, "prof": 1.00, "n": 4, "fr": 234, "al": [166, 179, 179], "lug": "Pared sur", "acc": "norte"},
]
_POR_COD = {r["c"]: r for r in RACKS}
# columnas con medidas propias (alto _a y fondo _p en cm)
DISTINTAS = {
    "B02": {"C05-N0_a": 175, "C05-N0_p": 100, "C05-N1_a": 135, "C05-N1_p": 100, "C05-N2_a": 135, "C05-N2_p": 100,
            "C06-N0_a": 175, "C06-N0_p": 100, "C06-N1_a": 135, "C06-N1_p": 100, "C06-N2_a": 135, "C06-N2_p": 99.8},
    "B03": {"C04-N0_a": 268, "C04-N0_p": 100, "C04-N1_a": 140, "C04-N1_p": 100, "C04-N2_a": 140, "C04-N2_p": 100},
    "B04": {"C03-N0_a": 204, "C03-N0_p": 100, "C03-N1_a": 133, "C03-N1_p": 100, "C03-N2_a": 133, "C03-N2_p": 100},
    "B06": {"C03-N0_a": 165, "C03-N0_p": 100, "C03-N1_a": 176, "C03-N1_p": 100, "C03-N2_a": 176, "C03-N2_p": 100},
}
PERSIANAS = {"P2": ("Persiana 2", 16.13, 20.98), "P1": ("Persiana 1", 39.60, 44.45)}   # sobre la pared oeste
SIN_CONFIRMAR = [{"x0": 44.45, "x1": 47.23, "z0": 0, "z1": 1.00, "t": "1,78 m sin confirmar"},
                 {"x0": 43.75, "x1": 48.40, "z0": 9.40, "z1": 10.40, "t": "3,35 m sin confirmar"}]
ZONAS = [("A", 0, 21.0), ("B", 21.0, 44.6), ("C", 44.6, NAVE_L)]
NOMBRE_NIVEL = {0: "planta baja", 1: "1er nivel", 2: "2do nivel"}
# en puntos del plano, como lo usa la ruta de devolución
PUERTAS = {k: (n, int(a * ESCALA), int(b * ESCALA)) for k, (n, a, b) in PERSIANAS.items()}

_COD = re.compile(r"^([A-Z])(\d{1,2})(?:-?C(\d{1,2}))?(?:-?N(\d))?$")   # "A-03" (formato viejo) no es A03


def partes(codigo: str) -> tuple[str, int, int | None] | None:
    """'B01-C07-N0' → ('B01', 7, 0); 'A03' → ('A03', 0, None). None si no es un lugar del plano."""
    m = _COD.match(re.sub(r"\s+", "", (codigo or "").upper()))
    if not m:
        return None
    rack = f"{m.group(1)}{int(m.group(2)):02d}"
    r = _POR_COD.get(rack)
    col = int(m.group(3) or 0)
    nivel = int(m.group(4)) if m.group(4) is not None else None
    if not r or col > r["n"] or (m.group(3) and not col) or (nivel is not None and (nivel >= NIVELES or not col)):
        return None
    return rack, col, nivel


def codigo(rack: str, col: int = 0, nivel: int | None = None) -> str:
    return rack + (f"-C{col:02d}" if col else "") + (f"-N{nivel}" if col and nivel is not None else "")


def canonico(cod: str) -> str:
    """'b1-c3-n1' → 'B01-C03-N1'. Vacío si no es del plano."""
    p = partes(cod)
    return codigo(*p) if p else ""


def describir(cod: str) -> str:
    p = partes(cod)
    if not p:
        return ""
    rack, col, nivel = p
    txt = [f"Zona {_POR_COD[rack]['z']}", f"rack {rack}"]
    if col:
        txt.append(f"columna {col}")
    if nivel is not None:
        txt.append(NOMBRE_NIVEL[nivel])
    return " · ".join(txt)


# ─────────────── Geometría (metros) ───────────────
def _rect(r: dict, s: float, e: float, c0: float, c1: float) -> tuple[float, float, float, float]:
    """(x0, x1, z0, z1) de un tramo del rack: s..e en su sentido, c0..c1 en el otro."""
    return (s, e, c0, c1) if r["d"] == "x" else (c0, c1, s, e)


def rect_rack(r: dict) -> tuple[float, float, float, float]:
    return _rect(r, r["a0"], r["a0"] + r["len"], r["c0"], r["c0"] + r["prof"])


def _acceso_m(cod: str) -> tuple[float, float] | None:
    p = partes(cod)
    if not p:
        return None
    r = _POR_COD[p[0]]
    paso = r["len"] / r["n"]
    medio = r["a0"] + ((p[1] - 0.5) * paso if p[1] else r["len"] / 2)
    x0, x1, z0, z1 = rect_rack(r)
    if r["d"] == "x":
        x = medio
        z = z1 + SEP_M if r["acc"] == "este" else z0 - SEP_M
    else:
        z = medio
        x = x1 + SEP_M if r["acc"] == "sur" else x0 - SEP_M
    return x, z


def _inicio_m(clave: str) -> tuple[float, float]:
    _, a, b = PERSIANAS.get(clave) or PERSIANAS["P1"]
    return (a + b) / 2, SEP_M


def a_plano(x: float, z: float) -> tuple[int, int]:
    return int(round(x * ESCALA)), int(round((NAVE_W - z) * ESCALA))


def acceso(cod: str) -> tuple[int, int] | None:
    """Punto del pasillo (en puntos del plano) desde donde se carga esa ubicación."""
    m = _acceso_m(cod)
    return a_plano(*m) if m else None


def inicio(clave: str) -> tuple[int, int]:
    return a_plano(*_inicio_m(clave))


# ─────────────── Caminar por los pasillos ───────────────
_GW, _GH = int(NAVE_L / CELDA_M), int(round(NAVE_W / CELDA_M))
_BLOQ: set[tuple[int, int]] = set()
for _r in RACKS:
    _x0, _x1, _z0, _z1 = rect_rack(_r)
    for gx in range(int(_x0 / CELDA_M), int((_x1 - 1e-6) / CELDA_M) + 1):
        for gz in range(int(_z0 / CELDA_M), int((_z1 - 1e-6) / CELDA_M) + 1):
            _BLOQ.add((gx, gz))


def _celda(p: tuple[float, float]) -> tuple[int, int]:
    c = (min(max(int(p[0] / CELDA_M), 0), _GW - 1), min(max(int(p[1] / CELDA_M), 0), _GH - 1))
    if c not in _BLOQ:
        return c
    cola, vistos = deque([c]), {c}  # la celda libre más cercana
    while cola:
        a = cola.popleft()
        for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            b = (a[0] + d[0], a[1] + d[1])
            if 0 <= b[0] < _GW and 0 <= b[1] < _GH and b not in vistos:
                if b not in _BLOQ:
                    return b
                vistos.add(b)
                cola.append(b)
    return c


def _bfs(origen: tuple[int, int]) -> dict[tuple[int, int], tuple[int, int] | None]:
    padre: dict[tuple[int, int], tuple[int, int] | None] = {origen: None}
    cola = deque([origen])
    while cola:
        a = cola.popleft()
        for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            b = (a[0] + d[0], a[1] + d[1])
            if 0 <= b[0] < _GW and 0 <= b[1] < _GH and b not in _BLOQ and b not in padre:
                padre[b] = a
                cola.append(b)
    return padre


def _camino(padre, destino) -> list[tuple[int, int]]:
    if destino not in padre:
        return []
    out, c = [], destino
    while c is not None:
        out.append(c)
        c = padre[c]
    return out[::-1]


def _simplificar(celdas: list[tuple[int, int]]) -> list[list[int]]:
    pts = [a_plano((c[0] + 0.5) * CELDA_M, (c[1] + 0.5) * CELDA_M) for c in celdas]
    if len(pts) < 3:
        return [list(p) for p in pts]
    out = [pts[0]]
    for a, b in zip(pts[1:], pts[2:]):
        prev = out[-1]
        if (a[0] - prev[0]) * (b[1] - a[1]) != (a[1] - prev[1]) * (b[0] - a[0]):
            out.append(a)
    out.append(pts[-1])
    return [list(p) for p in out]


def _metros(celdas: int) -> int:
    return round(max(celdas, 0) * CELDA_M)


def desde_punto(x: float, y: float) -> tuple[float, float]:
    """Punto del plano (puntos) → metros, para arrancar el camino desde donde está la persona."""
    return min(max(x / ESCALA, 0), NAVE_L), min(max(NAVE_W - y / ESCALA, 0), NAVE_W)


def camino(desde: tuple[float, float], hasta: str) -> dict:
    """Camino por los pasillos desde un punto (metros) hasta el pasillo de una ubicación."""
    fin = _acceso_m(hasta)
    if not fin:
        return {"tramo": [], "metros": 0}
    a, b = _celda(desde), _celda(fin)
    celdas = _camino(_bfs(a), b)
    return {"tramo": _simplificar(celdas), "metros": _metros(len(celdas) - 1), "punto": list(a_plano(*fin))}


def _origen_m(desde: str | tuple[float, float]) -> tuple[float, float]:
    """Persiana (P1 / P2) o un punto en metros (donde está la persona)."""
    return desde if isinstance(desde, tuple) else _inicio_m(desde)


def punto_origen(desde: str | tuple[float, float]) -> tuple[int, int]:
    return a_plano(*_origen_m(desde))


def ordenar(paradas: list[str], desde: str | tuple[float, float] = "P1") -> dict:
    """Mejor orden para recorrer las paradas (códigos de ubicación) saliendo de la persiana elegida o de un
    punto en metros. Devuelve {orden: [códigos], tramos: [[[x,y],…] por parada], metros_aprox}. Las que no
    están en el plano van al final, en el orden recibido."""
    en_mapa = [c for c in dict.fromkeys(paradas) if _acceso_m(c)]
    fuera = [c for c in dict.fromkeys(paradas) if not _acceso_m(c)]
    pts = [_celda(_origen_m(desde))] + [_celda(_acceso_m(c)) for c in en_mapa]
    padres = [_bfs(p) for p in pts]
    n = len(pts)
    INF = 10 ** 9
    dist = [[(len(_camino(padres[i], pts[j])) - 1) if pts[j] in padres[i] else INF for j in range(n)] for i in range(n)]
    resto = list(range(1, n))
    if len(resto) <= 12:  # exacto (Held-Karp): camino abierto que sale de la persiana
        m = len(resto)
        dp: dict[tuple[int, int], tuple[int, int]] = {}
        for i in range(m):
            dp[(1 << i, i)] = (dist[0][resto[i]], -1)
        for mask in range(1, 1 << m):
            for i in range(m):
                if (mask, i) not in dp:
                    continue
                c0 = dp[(mask, i)][0]
                for j in range(m):
                    if mask & (1 << j):
                        continue
                    k = (mask | (1 << j), j)
                    c = c0 + dist[resto[i]][resto[j]]
                    if k not in dp or c < dp[k][0]:
                        dp[k] = (c, i)
        orden = []
        if m:
            full = (1 << m) - 1
            i = min(range(m), key=lambda x: dp[(full, x)][0])
            mask = full
            while i != -1:
                orden.append(resto[i])
                i, mask = dp[(mask, i)][1], mask & ~(1 << i)
            orden.reverse()
    else:  # vecino más cercano + 2-opt
        orden, act, libres = [], 0, set(resto)
        while libres:
            act = min(libres, key=lambda j: dist[act][j])
            orden.append(act)
            libres.discard(act)
        mejoro = True
        while mejoro:
            mejoro = False
            for i in range(len(orden) - 1):
                for j in range(i + 1, len(orden)):
                    nuevo = orden[:i] + orden[i:j + 1][::-1] + orden[j + 1:]
                    if sum(dist[a][b] for a, b in zip([0] + nuevo, nuevo)) < sum(dist[a][b] for a, b in zip([0] + orden, orden)):
                        orden, mejoro = nuevo, True
    tramos, total, prev = [], 0, 0
    for k in orden:
        tramos.append(_simplificar(_camino(padres[prev], pts[k])))
        total += dist[prev][k]
        prev = k
    return {"orden": [en_mapa[k - 1] for k in orden] + fuera, "tramos": tramos + [[] for _ in fuera],
            "metros_aprox": _metros(total)}


def plano() -> dict:
    """Lo necesario para dibujar el plano: en puntos (vista de arriba) y en metros (visor 3D)."""
    racks = []
    for r in RACKS:
        x0, x1, z0, z1 = rect_rack(r)
        px, py = a_plano(x0, z1)
        racks.append({"codigo": r["c"], "x": px, "y": py, "w": round((x1 - x0) * ESCALA), "h": round((z1 - z0) * ESCALA),
                      "zona": r["z"], "columnas": r["n"], "plantas": NIVELES, "vertical": r["d"] == "z"})
    return {"ancho": ANCHO, "alto": ALTO, "escala": ESCALA,
            "zonas": [{"zona": z, "x": int(a * ESCALA), "w": int((b - a) * ESCALA)} for z, a, b in ZONAS],
            "racks": racks,
            "puertas": [{"clave": k, "nombre": n, "x": a, "w": b - a} for k, (n, a, b) in PUERTAS.items()],
            "deposito": {"nave": {"L": NAVE_L, "W": NAVE_W}, "racks": RACKS, "distintas": DISTINTAS,
                         "persianas": [{"clave": k, "n": n, "x0": a, "x1": b} for k, (n, a, b) in PERSIANAS.items()],
                         "sin_confirmar": SIN_CONFIRMAR}}


def codigos_iniciales() -> list[dict]:
    """Todos los módulos del plano (rack · columna · nivel) para cargar en Mapeo, en orden de recorrido."""
    out = []
    for r in RACKS:
        for c in range(1, r["n"] + 1):
            for n in range(NIVELES):
                cod = codigo(r["c"], c, n)
                out.append({"codigo": cod, "zona": r["z"], "pasillo": r["c"], "modulo": c, "nivel": n + 1,
                            "descripcion": describir(cod)})
    out.sort(key=lambda o: (_acceso_m(o["codigo"])[0], _acceso_m(o["codigo"])[1], o["nivel"]))
    return out
