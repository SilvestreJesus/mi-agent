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

# Archivo de persistencia de conversaciones
SESSIONS_FILE = "sessions_history.json"
sessions_db: Dict[str, Any] = {}
active_session_agents: Dict[str, Any] = {}
active_session_files: Dict[str, List[str]] = {} 
active_session_images: Dict[str, List[str]] = {} 
active_session_source_ids: Dict[str, str] = {}

file_lock = asyncio.Lock()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tiff"}
URL_IMAGE_PATTERN = re.compile(r'https?://[^\s]+\.(?:jpg|jpeg|png|gif|webp|bmp|svg|tiff)(?:\?[^\s]*)?', re.IGNORECASE)

# Configuración de URLs y Credenciales desde Entorno / .env
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://mcp-server:8000/mcp")
MCP_HTTP_BASE = MCP_SERVER_URL[:-4] if MCP_SERVER_URL.endswith("/mcp") else MCP_SERVER_URL

JUB_URL = os.environ.get("JUB_API_URL", "http://localhost:5000")
JUB_USER = os.environ.get("JUB_USERNAME", "invitado")
JUB_PASS = os.environ.get("JUB_PASSWORD", "invitado")

# Directorios de volúmenes compartidos
SHARED_UPLOAD_DIR = os.environ.get("SHARED_UPLOAD_DIR", "/app/sources")
SHARED_IMAGE_DIR = os.environ.get("SHARED_IMAGE_DIR", "/app/images")

os.makedirs(SHARED_UPLOAD_DIR, exist_ok=True)
os.makedirs(SHARED_IMAGE_DIR, exist_ok=True)

JSON_FILENAME_PATTERN = re.compile(r'([\w\-]+\.json)')


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


def extraer_archivos_json(texto: str) -> List[str]:
    return list(dict.fromkeys(JSON_FILENAME_PATTERN.findall(texto)))


async def _get_jub_token() -> Optional[str]:
    """Helper para autenticarse en JUB API y obtener el token de acceso."""
    try:
        async with httpx.AsyncClient(base_url=JUB_URL, timeout=10.0) as client:
            auth_res = await client.post(
                "/api/v2/users/auth",
                json={"username": JUB_USER, "password": JUB_PASS},
            )
            if auth_res.status_code in (200, 201):
                data = auth_res.json()
                return data.get("access_token") or data.get("token")
    except Exception:
        pass
    return None


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

    # 1. Interceptar preguntas sobre el estado de conexión del servidor JUB
    if not file and any(k in msg_lower for k in ["está conectado", "esta conectado", "conexión activa", "conexion activa", "backend"]):
        token = await _get_jub_token()
        conexion_exitosa = token is not None

        if conexion_exitosa:
            respuesta_texto = f"── Conexión con JUB activa ─────────────────────────────\n\nEl sistema está correctamente conectado a la API de JUB en {JUB_URL}."
        else:
            respuesta_texto = f"── Sin conexión con el servidor ────────────────────────\n\nNo fue posible establecer comunicación con el servidor JUB en {JUB_URL}."

        session_data["messages"].append({"role": "user", "content": message})
        session_data["messages"].append({"role": "assistant", "content": respuesta_texto})
        await save_sessions_async()

        return ChatResponse(
            session_id=session_id,
            title=session_data["title"],
            text=respuesta_texto,
            downloads=None,
        )

    # 2. Interceptar solicitudes para listar observatorios consultando directamente la API de JUB
    if not file and any(k in msg_lower for k in ["observatorio", "observatorios", "qué observatorios hay", "lista los observatorios", "que observatorios existen"]):
        token = await _get_jub_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        
        observatorios_lista = []
        try:
            async with httpx.AsyncClient(base_url=JUB_URL, timeout=10.0) as client:
                res = await client.get("/api/v2/observatories", headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    observatorios_lista = data if isinstance(data, list) else data.get("observatories", [])
        except Exception:
            pass

        respuesta_texto = "── Observatorios Registrados en JUB ───────────────────\n\n"
        if observatorios_lista:
            for obs in observatorios_lista:
                obs_id = obs.get("observatory_id") or obs.get("id") or "N/D"
                obs_title = obs.get("title") or obs.get("name") or "Sin título"
                respuesta_texto += f"• **{obs_title}** (ID: `{obs_id}`)\n"
        else:
            respuesta_texto += "No se pudieron recuperar observatorios activos o la lista está vacía actualmente en la API.\n"

        session_data["messages"].append({"role": "user", "content": message})
        session_data["messages"].append({"role": "assistant", "content": respuesta_texto})
        await save_sessions_async()

        return ChatResponse(
            session_id=session_id,
            title=session_data["title"],
            text=respuesta_texto,
            downloads=None,
        )

    # 3. Interceptar solicitud de Auditoría mostrando ÚNICAMENTE archivos JSON (ocultando CSVs)
    if not file and any(k in msg_lower for k in ["archivos json", "json generados", "muéstrame los archivos"]):
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

    # 4. Flujo principal del Agente de Inteligencia Artificial (Ollama + MCP Tools)
    if session_id not in active_session_agents:
        agent = build_agent()
        await agent.__aenter__()
        active_session_agents[session_id] = agent

    agent = active_session_agents[session_id]

    file_info_list = []
    
    # ── Manejo de Archivos Subidos Físicamente (FormData) ──
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

    # ── Detección automática de URLs de Imágenes web (Pexels, Pixabay, etc.) ──
    detected_urls = URL_IMAGE_PATTERN.findall(message)
    if detected_urls:
        if session_id not in active_session_images:
            active_session_images[session_id] = []
            
        async with httpx.AsyncClient(timeout=30.0) as url_client:
            for url_img in detected_urls:
                try:
                    img_resp = await url_client.get(url_img)
                    if img_resp.status_code == 200:
                        parsed_name = url_img.split("?")[0].split("/")[-1]
                        if not any(parsed_name.endswith(ext) for ext in IMAGE_EXTENSIONS):
                            parsed_name = f"web_image_{uuid.uuid4().hex[:6]}.jpg"
                        
                        img_path = os.path.join(SHARED_IMAGE_DIR, parsed_name)
                        async with aiofiles.open(img_path, "wb") as out_img:
                            await out_img.write(img_resp.content)
                        
                        if parsed_name not in active_session_images[session_id]:
                            active_session_images[session_id].append(parsed_name)
                        
                        file_info_list.append(f"[Imagen externa descargada desde URL y guardada en '/app/images/{parsed_name}'. URL original: {url_img}]")
                except Exception as e:
                    file_info_list.append(f"[Aviso: No se pudo descargar la imagen desde la URL {url_img}: {str(e)}]")

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