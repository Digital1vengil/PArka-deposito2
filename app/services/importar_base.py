"""Copia una base de PARKA Depósito (archivo SQLite del ejecutable) a la base actual (Postgres en la nube).

Se usa una sola vez al pasar a Cloudflare: Admin → Importar base, o por consola:
    python -m scripts.migrar_base ruta/parka_deposito.db
Reemplaza TODO lo que haya en la base de destino.
"""
from __future__ import annotations

import logging

from sqlalchemy import Engine, Integer, create_engine, inspect, select, text

from .. import models  # noqa: F401  (registra las tablas en Base.metadata)
from ..db import Base, engine as engine_actual, migrar

log = logging.getLogger("parka")
LOTE = 1000


def _migrar_origen(src: Engine) -> None:
    """Si el archivo es de una versión anterior, le agrega las columnas que falten (sin tocar el archivo original:
    se trabaja sobre la copia temporal que subió el admin)."""
    insp = inspect(src)
    tablas = set(insp.get_table_names())
    with src.begin() as con:
        for t in Base.metadata.sorted_tables:
            if t.name not in tablas:
                continue
            existentes = {c["name"] for c in insp.get_columns(t.name)}
            for col in t.columns:
                if col.name not in existentes:
                    tipo = col.type.compile(dialect=src.dialect)
                    con.execute(text(f'ALTER TABLE "{t.name}" ADD COLUMN "{col.name}" {tipo}'))


def copiar(origen_url: str, destino: Engine | None = None) -> dict:
    destino = destino or engine_actual
    src = create_engine(origen_url)
    tablas_src = set(inspect(src).get_table_names())
    if "usuarios" not in tablas_src:
        raise ValueError("El archivo no parece una base de PARKA Depósito (no tiene la tabla de usuarios).")
    _migrar_origen(src)

    Base.metadata.create_all(destino)
    if destino is engine_actual:
        migrar()

    resumen: dict[str, int] = {}
    arreglos: dict[str, int] = {}
    ids: dict[str, set] = {}
    tablas = Base.metadata.sorted_tables
    with src.connect() as s, destino.begin() as d:
        for t in reversed(tablas):
            d.execute(t.delete())
        for t in tablas:
            if t.name not in tablas_src:
                continue
            filas = s.execute(select(*t.columns)).mappings()
            # SQLite no controlaba las referencias: un id que apunta a algo que no existe (p. ej. 0) queda vacío
            fks = [(fk.parent.name, fk.column.table.name, fk.parent.nullable)
                   for fk in t.foreign_keys if fk.column.table.name in ids]
            pk = list(t.primary_key.columns)
            ids[t.name] = set()
            n = 0
            lote: list[dict] = []
            for f in filas:
                fila = dict(f)
                saltar = False
                for col, padre, nulo in fks:
                    v = fila.get(col)
                    if v is not None and v not in ids[padre]:
                        if nulo:
                            fila[col] = None
                            arreglos[f"{t.name}.{col}"] = arreglos.get(f"{t.name}.{col}", 0) + 1
                        else:
                            saltar = True
                if saltar:
                    arreglos[f"{t.name} (fila huérfana omitida)"] = arreglos.get(f"{t.name} (fila huérfana omitida)", 0) + 1
                    continue
                if len(pk) == 1:
                    ids[t.name].add(fila[pk[0].name])
                lote.append(fila)
                if len(lote) >= LOTE:
                    d.execute(t.insert(), lote)
                    n += len(lote)
                    lote = []
            if lote:
                d.execute(t.insert(), lote)
                n += len(lote)
            resumen[t.name] = n

        # Postgres: los contadores de id siguen desde el último número copiado
        if d.dialect.name == "postgresql":
            for t in tablas:
                for c in t.primary_key.columns:
                    if isinstance(c.type, Integer):
                        d.execute(text(
                            f"SELECT setval(pg_get_serial_sequence('\"{t.name}\"', '{c.name}'), "
                            f"COALESCE((SELECT MAX(\"{c.name}\") FROM \"{t.name}\"), 0) + 1, false)"))
    src.dispose()
    log.info("Base importada: %s · referencias arregladas: %s", resumen, arreglos)
    if arreglos:
        resumen["_referencias_arregladas"] = sum(arreglos.values())
    return resumen
