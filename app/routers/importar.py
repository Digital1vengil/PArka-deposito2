"""Admin → Importar base: sube el archivo parka_deposito.db del ejecutable y lo copia a la base de la nube."""
import os
import tempfile

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse

from ..auth import solo_admin
from ..models import Usuario
from ..templating import render

router = APIRouter(prefix="/admin")


@router.get("/importar-base")
def pagina(request: Request, u: Usuario = Depends(solo_admin)):
    return render(request, "admin_importar.html", u=u)


@router.post("/api/importar-base")
async def importar(archivo: UploadFile = File(...), confirmo: str = "", u: Usuario = Depends(solo_admin)):
    from ..services.importar_base import copiar
    datos = await archivo.read()
    if not datos.startswith(b"SQLite format 3"):
        return JSONResponse({"error": "El archivo no es una base SQLite (tiene que ser parka_deposito.db)."},
                            status_code=400)
    fd, ruta = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(datos)
        try:
            resumen = copiar(f"sqlite:///{ruta}")
            from ..services import ajustes
            ajustes.olvidar()
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"No se pudo importar: {e}"}, status_code=400)
    finally:
        try:
            os.remove(ruta)
        except OSError:
            pass
    return {"ok": True, "tablas": resumen, "total": sum(v for k, v in resumen.items() if not k.startswith("_"))}
