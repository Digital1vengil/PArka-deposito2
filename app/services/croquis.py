"""Croquis del depósito MAGONTEX y ruta para guardar las devoluciones.

Coordenadas en "puntos" del croquis (0,0 = esquina Norte de arriba). El depósito mide 1171 x 203; las racks son
rectángulos y cada una tiene su punto de acceso (el pasillo desde donde se la carga). B01 está dividido en 8
columnas (C01..C08, de izquierda a derecha) y 3 plantas (N0 = planta baja, N1, N2); su código es B01-C07-N0.
La ruta se calcula caminando por los pasillos (grilla con las racks como obstáculos), no en línea recta.
"""
from __future__ import annotations

import re
from collections import deque

ANCHO, ALTO = 1171, 203
CELDA = 8  # grilla para caminar

# código: (x, y, ancho, alto, zona, lado de acceso)
RACKS: dict[str, tuple[int, int, int, int, str, str]] = {
    "A04": (0, 0, 405, 25, "A", "abajo"),
    "A05": (0, 50, 24, 105, "A", "derecha"),
    "A03": (50, 81, 270, 25, "A", "arriba"),
    "A02": (50, 106, 270, 25, "A", "abajo"),
    "A01": (0, 178, 314, 25, "A", "arriba"),
    "B06": (413, 0, 24, 129, "B", "derecha"),
    "B04": (600, 0, 135, 25, "B", "abajo"),
    "B05": (770, 0, 90, 25, "B", "abajo"),
    "B03": (437, 80, 315, 25, "B", "arriba"),
    "B02": (437, 105, 315, 24, "B", "abajo"),
    "B01": (436, 178, 359, 25, "B", "arriba"),
    "C04": (885, 0, 135, 25, "C", "abajo"),
    "C03": (888, 75, 136, 24, "C", "arriba"),
    "C02": (888, 100, 136, 24, "C", "abajo"),
    "C01": (951, 178, 135, 25, "C", "arriba"),
    "C05": (1147, 17, 24, 186, "C", "izquierda"),
}
COLUMNAS = {"B01": 8}       # racks divididos en columnas (de izquierda a derecha)
PLANTAS = {"B01": 3}        # plantas: N0 (baja), N1, N2
ZONAS = [("A", 0, 412), ("B", 412, 878), ("C", 878, ANCHO)]
PUERTAS = {"P2": ("Persiana 2", 315, 410), "P1": ("Persiana 1", 837, 930)}
NOMBRE_PLANTA = {0: "planta baja", 1: "planta 1", 2: "planta 2"}
SEP = 14  # distancia del punto de acceso al frente del rack

_COD = re.compile(r"^([A-Z]\d{2})(?:-C(\d{1,2}))?(?:-N(\d))?$")


def partes(codigo: str) -> tuple[str, int, int | None] | None:
    """'B01-C07-N0' → ('B01', 7, 0); 'A03' → ('A03', 0, None). None si no es un rack del croquis."""
    m = _COD.match((codigo or "").strip().upper())
    if not m or m.group(1) not in RACKS:
        return None
    return m.group(1), int(m.group(2) or 0), (int(m.group(3)) if m.group(3) is not None else None)


def describir(codigo: str) -> str:
    p = partes(codigo)
    if not p:
        return ""
    rack, col, planta = p
    txt = [f"Zona {RACKS[rack][4]}", f"rack {rack}"]
    if col:
        txt.append(f"columna {col}")
    if planta is not None:
        txt.append(NOMBRE_PLANTA.get(planta, f"planta {planta}"))
    return " · ".join(txt)


def acceso(codigo: str) -> tuple[int, int] | None:
    """Punto del pasillo desde donde se carga esa ubicación."""
    p = partes(codigo)
    if not p:
        return None
    rack, col, _ = p
    x, y, w, h, _, lado = RACKS[rack]
    cx, cy = x + w / 2, y + h / 2
    n = COLUMNAS.get(rack, 0)
    if col and n:
        col = min(max(col, 1), n)
        if w >= h:
            cx = x + (col - 0.5) * w / n
        else:
            cy = y + (col - 0.5) * h / n
    if lado == "abajo":
        return int(cx), y + h + SEP
    if lado == "arriba":
        return int(cx), y - SEP
    if lado == "derecha":
        return x + w + SEP, int(cy)
    return x - SEP, int(cy)


def inicio(clave: str) -> tuple[int, int]:
    _, a, b = PUERTAS.get(clave) or PUERTAS["P1"]
    return (a + b) // 2, ALTO - SEP


# ─────────────── Caminar por los pasillos ───────────────
_GW, _GH = ANCHO // CELDA + 1, ALTO // CELDA + 1
_BLOQ: set[tuple[int, int]] = set()
for _x, _y, _w, _h, _z, _l in RACKS.values():
    for gx in range(_x // CELDA, (_x + _w - 1) // CELDA + 1):
        for gy in range(_y // CELDA, (_y + _h - 1) // CELDA + 1):
            _BLOQ.add((gx, gy))


def _celda(p: tuple[int, int]) -> tuple[int, int]:
    c = (min(max(p[0] // CELDA, 0), _GW - 1), min(max(p[1] // CELDA, 0), _GH - 1))
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
    pts = [(c[0] * CELDA + CELDA // 2, c[1] * CELDA + CELDA // 2) for c in celdas]
    if len(pts) < 3:
        return [list(p) for p in pts]
    out = [pts[0]]
    for a, b in zip(pts[1:], pts[2:]):
        prev = out[-1]
        if (a[0] - prev[0]) * (b[1] - a[1]) != (a[1] - prev[1]) * (b[0] - a[0]):
            out.append(a)
    out.append(pts[-1])
    return [list(p) for p in out]


def ordenar(paradas: list[str], desde: str = "P1") -> dict:
    """Mejor orden para recorrer las paradas (códigos de ubicación) saliendo de la persiana elegida.
    Devuelve {orden: [códigos], tramos: [[[x,y],…] por parada], metros_aprox}. Las que no están en el
    croquis van al final, en el orden recibido."""
    en_mapa = [c for c in dict.fromkeys(paradas) if acceso(c)]
    fuera = [c for c in dict.fromkeys(paradas) if not acceso(c)]
    pts = [_celda(inicio(desde))] + [_celda(acceso(c)) for c in en_mapa]
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
            "metros_aprox": round(total * CELDA / 40)}  # el croquis mide ~29 m de largo (1171 / 40)


def plano() -> dict:
    """Lo necesario para dibujar el croquis en pantalla."""
    return {"ancho": ANCHO, "alto": ALTO,
            "zonas": [{"zona": z, "x": a, "w": b - a} for z, a, b in ZONAS],
            "racks": [{"codigo": c, "x": x, "y": y, "w": w, "h": h, "zona": z, "columnas": COLUMNAS.get(c, 0),
                       "plantas": PLANTAS.get(c, 0)} for c, (x, y, w, h, z, _) in RACKS.items()],
            "puertas": [{"clave": k, "nombre": n, "x": a, "w": b - a} for k, (n, a, b) in PUERTAS.items()]}


def codigos_iniciales() -> list[dict]:
    """Las ubicaciones del croquis para cargar en Mapeo (una por rack; B01 por columna y planta)."""
    out = []
    for rack, (x, y, w, h, zona, _) in RACKS.items():
        cols, plantas = COLUMNAS.get(rack, 0), PLANTAS.get(rack, 0)
        if not cols:
            out.append({"codigo": rack, "zona": zona, "pasillo": rack, "modulo": 0, "nivel": 0})
            continue
        for c in range(1, cols + 1):
            for p in range(plantas or 1):
                cod = f"{rack}-C{c:02d}" + (f"-N{p}" if plantas else "")
                out.append({"codigo": cod, "zona": zona, "pasillo": rack, "modulo": c,
                            "nivel": p + 1 if plantas else 0})
    for o in out:
        o["descripcion"] = describir(o["codigo"])
    return out
