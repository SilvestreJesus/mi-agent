import os
import json
import re
import uuid
import asyncio
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any

import httpx
import aiofiles
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from tutor_agent import build_agent

SESSIONS_FILE = "sessions_history.json"
sessions_db: Dict[str, Any] = {}
active_session_agents: Dict[str, Any] = {}
active_session_files: Dict[str, List[str]] = {} 
active_session_images: Dict[str, List[str]] = {} 
active_session_source_ids: Dict[str, str] = {}

file_lock = asyncio.Lock()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tiff"}

def load_sessions() -> dict:
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

async def save_sessions_async():
    """Guarda las sesiones de forma asíncrona evitando bloqueos de I/O."""
    async with file_lock:
        async with aiofiles.open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(sessions_db, ensure_ascii=False, indent=2))

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://mcp-server:8000/mcp")
MCP_HTTP_BASE = MCP_SERVER_URL[:-4] if MCP_SERVER_URL.endswith("/mcp") else MCP_SERVER_URL

# Directorios de volúmenes compartidos
SHARED_UPLOAD_DIR = os.environ.get("SHARED_UPLOAD_DIR", "/app/sources")
SHARED_IMAGE_DIR = os.environ.get("SHARED_IMAGE_DIR", "/app/images")

os.makedirs(SHARED_UPLOAD_DIR, exist_ok=True)
os.makedirs(SHARED_IMAGE_DIR, exist_ok=True)

JSON_FILENAME_PATTERN = re.compile(r'([\w\-]+\.json)')

def extraer_archivos_json(texto: str) -> List[str]:
    return list(dict.fromkeys(JSON_FILENAME_PATTERN.findall(texto)))

async def close_agent_session(session_id: str):
    """Cierra limpiamente el cliente MCP asociado a la sesión."""
    agent = active_session_agents.pop(session_id, None)
    if agent:
        try:
            await agent.__aexit__(None, None, None)
        except Exception:
            pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    global sessions_db
    sessions_db = load_sessions()
    yield
    await save_sessions_async()
    for sid in list(active_session_agents.keys()):
        await close_agent_session(sid)

app = FastAPI(title="JUB Agent with Pipeline Integration", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class DownloadItem(BaseModel):
    name: str
    url: str

class ChatResponse(BaseModel):
    session_id: str
    title: str
    text: str
    downloads: Optional[List[DownloadItem]] = None

class SessionSummary(BaseModel):
    id: str
    title: str

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/sessions", response_model=List[SessionSummary])
async def list_sessions():
    return [{"id": sid, "title": data.get("title", "Nueva conversación")} for sid, data in sessions_db.items()]

@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    if session_id not in sessions_db:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    return sessions_db[session_id]

@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    if session_id in sessions_db:
        del sessions_db[session_id]
        await save_sessions_async()
    
    await close_agent_session(session_id)
    active_session_files.pop(session_id, None)
    active_session_images.pop(session_id, None)
    active_session_source_ids.pop(session_id, None)
    
    return {"status": "deleted"}

@app.get("/download/{filename}")
async def descargar_archivo(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")

    url = f"{MCP_HTTP_BASE}/files/{filename}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar al servidor MCP: {e}")

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="No se pudo obtener el archivo")

    return StreamingResponse(
        iter([resp.content]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

async def listar_archivos_mcp(source_id: Optional[str] = None) -> List[dict]:
    url = f"{MCP_HTTP_BASE}/files"
    params = {"source_id": source_id} if source_id else None
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp.json().get("files", [])

@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: Request,
    message: str = Form(...),
    file: Optional[List[UploadFile]] = File(None),
    session_id: Optional[str] = Form(None)
):
    if not session_id or session_id not in sessions_db:
        session_id = str(uuid.uuid4())
        title = message[:30] + ("..." if len(message) > 30 else "")
        sessions_db[session_id] = {
            "title": title,
            "messages": []
        }

    session_data = sessions_db[session_id]
    base = str(request.base_url).rstrip("/")
    msg_lower = message.lower()

    # 1. Interceptar solicitud de Estado de Conexión o Listado de Observatorios
    if not file and any(k in msg_lower for k in ["conectado", "conexion", "conexión", "observatorios", "observatorio", "indexados", "existen"]):
        obs_url = f"{MCP_HTTP_BASE}/observatories"
        observatorios_info = []
        conexion_exitosa = False
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(obs_url)
                if res.status_code == 200:
                    conexion_exitosa = True
                    observatorios_info = res.json().get("observatories", [])
        except Exception:
            pass

        if not observatorios_info and os.path.exists("state.json"):
            try:
                with open("state.json", "r", encoding="utf-8") as f:
                    observatorios_info = [json.load(f)]
                conexion_exitosa = True
            except Exception:
                pass

        if conexion_exitosa or observatorios_info:
            respuesta_texto = "── Conexión con JUB activa ─────────────────────────────\n\n"
            if observatorios_info:
                respuesta_texto += f"Se encontraron {len(observatorios_info)} observatorio(s) registrado(s):\n\n"
                for o in observatorios_info:
                    obs_id = o.get("observatory_id") or o.get("id") or "N/A"
                    obs_title = o.get("title") or o.get("observatory_title") or "Sin título"
                    institution = o.get("metadata", {}).get("institution") or o.get("institution") or "N/A"
                    edition = o.get("metadata", {}).get("edition") or o.get("edition") or "N/A"
                    respuesta_texto += f"• ID: {obs_id}\n  Título: {obs_title}\n  Institución: {institution} ({edition})\n\n"
            else:
                respuesta_texto += "No se encontraron observatorios registrados actualmente en el sistema."
        else:
            respuesta_texto = "── Sin conexión con el servidor ────────────────────────\n\nNo fue posible establecer comunicación con el servidor MCP de JUB."

        session_data["messages"].append({"role": "user", "content": message})
        session_data["messages"].append({"role": "assistant", "content": respuesta_texto})
        await save_sessions_async()

        return ChatResponse(
            session_id=session_id,
            title=session_data["title"],
            text=respuesta_texto,
            downloads=None,
        )

    # 2. Interceptar solicitud de Auditoría limitándola ÚNICAMENTE a Archivos JSON Generados (sin mostrar CSVs)
    if not file and any(k in msg_lower for k in ["archivos", "json", "generados", "muéstrame", "lista"]):
        json_archivos = []
        try:
            mcp_files = await listar_archivos_mcp()
            json_archivos = [f["name"] for f in mcp_files if f["name"].endswith(".json")]
        except Exception:
            pass

        respuesta_texto = "── Auditoría de Archivos JSON Generados ───────────────\n\n"
        downloads = []
        
        if json_archivos:
            respuesta_texto += "📄 Archivos JSON disponibles para descarga:\n"
            for j in json_archivos:
                respuesta_texto += f"  - {j}\n"
                downloads.append(DownloadItem(name=j, url=f"{base}/download/{j}"))
        else:
            respuesta_texto += "  (Ningún archivo JSON generado todavía en el sistema)\n"
            downloads = None

        session_data["messages"].append({"role": "user", "content": message})
        session_data["messages"].append({"role": "assistant", "content": respuesta_texto})
        await save_sessions_async()

        return ChatResponse(
            session_id=session_id,
            title=session_data["title"],
            text=respuesta_texto,
            downloads=downloads if downloads else None,
        )

    # Inicializar agente si no está activo para otras consultas o flujos de IA
    if session_id not in active_session_agents:
        agent = build_agent()
        await agent.__aenter__()
        active_session_agents[session_id] = agent

    agent = active_session_agents[session_id]

    file_info_list = []
    
    if file:
        if session_id not in active_session_files:
            active_session_files[session_id] = []
        if session_id not in active_session_images:
            active_session_images[session_id] = []
            
        for f in file:
            if f.filename: 
                ext = os.path.splitext(f.filename)[1].lower()

                if ext in IMAGE_EXTENSIONS:
                    file_path = os.path.join(SHARED_IMAGE_DIR, f.filename)
                    async with aiofiles.open(file_path, "wb") as out_file:
                        while chunk := await f.read(1024 * 1024):
                            await out_file.write(chunk)

                    active_session_images[session_id].append(f.filename)
                    file_info_list.append(f"[Imagen adjunta guardada con éxito en '/app/images/{f.filename}']")
                else:
                    file_path = os.path.join(SHARED_UPLOAD_DIR, f.filename)
                    async with aiofiles.open(file_path, "wb") as out_file:
                        while chunk := await f.read(1024 * 1024):
                            await out_file.write(chunk)

                    active_session_files[session_id].append(f.filename)
                    file_info_list.append(f"[Archivo CSV guardado exitosamente en '/app/sources/{f.filename}'. Usa 'csv_filename=\"{f.filename}\"']")

    elif session_id in active_session_files or session_id in active_session_images:
        for filename in active_session_files.get(session_id, []):
            if os.path.exists(os.path.join(SHARED_UPLOAD_DIR, filename)):
                file_info_list.append(f"[Recordatorio de contexto: El archivo CSV '{filename}' está disponible en '/app/sources/']")
        for imgname in active_session_images.get(session_id, []):
             if os.path.exists(os.path.join(SHARED_IMAGE_DIR, imgname)):
                file_info_list.append(f"[Recordatorio de contexto: La imagen '{imgname}' está disponible en '/app/images/']")

    file_info = "\n\n" + "\n".join(file_info_list) if file_info_list else ""
    prompt_completo = message + file_info

    result = await agent.run(prompt_completo)
    respuesta_texto = getattr(result, "text", str(result))

    nombres_json = extraer_archivos_json(respuesta_texto)
    downloads = None
    if nombres_json:
        downloads = [DownloadItem(name=n, url=f"{base}/download/{n}") for n in nombres_json]

    session_data["messages"].append({"role": "user", "content": message})
    session_data["messages"].append({"role": "assistant", "content": respuesta_texto})
    await save_sessions_async()

    return ChatResponse(
        session_id=session_id,
        title=session_data["title"],
        text=respuesta_texto,
        downloads=downloads
    )