from datetime import datetime, date

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL, TZ

_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_args, pool_pre_ping=True)
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
    with engine.begin() as con:
        for tabla in Base.metadata.sorted_tables:
            if tabla.name not in tablas:
                continue
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
