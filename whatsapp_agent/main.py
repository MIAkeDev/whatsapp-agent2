"""
Punto de entrada principal — Railway ejecuta este archivo.
"""
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from api.main import app

DASHBOARD_DIR = Path(__file__).parent / "dashboard"
app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")


@app.get("/")
async def dashboard():
    return FileResponse(str(DASHBOARD_DIR / "index.html"))
