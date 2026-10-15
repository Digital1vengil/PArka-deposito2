from datetime import datetime, date

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL, TZ

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False}, pool_pre_ping=True)
else:
    # Postgres (Neon): pool más grande para los pedidos en paralelo + tareas automáticas, conexiones que se
    # renuevan antes de que Neon las corte por inactividad, keepalive TCP y timeout de conexión acotado
    # (si la base tarda en despertar, el pedido falla con un error claro en vez de quedar colgado).
    engine = create_engine(
        DATABASE_URL, pool_pre_ping=True, pool_size=8, max_overflow=12, pool_recycle=240, pool_timeout=20,
        connect_args={"connect_timeout": 15, "keepalives": 1, "keepalives_idle": 30,
                      "keepalives_interval": 10, "keepalives_count": 3, "application_name": "parka-deposito"})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ahora() -> datetime:
    """Hora actual de Buenos Aires, sin tzinfo (se guarda así en la base)."""
    return datetime.now(TZ).replace(tzinfo=None)


def hoy() -> date:
    return ahora().date()


def migrar() -> None:
    """Agrega columnas nuevas a tablas existentes (la base de una versión anterior sigue sirviendo)."""
    from sqlalchemy import inspect, text, Boolean, Integer
    insp = inspect(engine)
    tablas = set(insp.get_table_names())
    # todas las columnas en una sola consulta (antes: una por tabla, ~35 viajes a Neon en cada arranque)
    try:
        columnas = {t: {c["name"] for c in cols} for (_, t), cols in insp.get_multi_columns().items()}
    except (AttributeError, NotImplementedError):
        columnas = {}
    with engine.begin() as con:
        for tabla in Base.metadata.sorted_tables:
            if tabla.name not in tablas:
                continue
            existentes = columnas.get(tabla.name)
            if existentes is None:
                existentes = {c["name"] for c in insp.get_columns(tabla.name)}
            for col in tabla.columns:
                if col.name in existentes:
                    continue
                tipo = col.type.compile(dialect=engine.dialect)
                if isinstance(col.type, Boolean):
                    defecto = " DEFAULT FALSE" if engine.dialect.name == "postgresql" else " DEFAULT 0"
                elif isinstance(col.type, Integer):
                    defecto = " DEFAULT 0"
                elif col.nullable:
                    defecto = ""
                else:
                    defecto = " DEFAULT ''"
                con.execute(text(f'ALTER TABLE {tabla.name} ADD COLUMN {col.name} {tipo}{defecto}'))
