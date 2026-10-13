"""Copia una base SQLite del ejecutable a la base indicada en DATABASE_URL (por consola).

    set DATABASE_URL=postgresql://...neon.tech/neondb?sslmode=require
    python -m scripts.migrar_base "C:\\ruta\\parka_deposito.db"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.importar_base import copiar  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    r = copiar(f"sqlite:///{Path(sys.argv[1]).resolve()}")
    for t, n in r.items():
        print(f"{t:28} {n}")
    print("Total:", sum(v for k, v in r.items() if not k.startswith("_")))
