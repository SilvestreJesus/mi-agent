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


# ============================================================
# CONFIGURACIÓN
# ============================================================

SESSIONS_FILE = "sessions_history.json"

sessions_db: Dict[str, Any] = {}
active_session_agents: Dict[str, Any] = {}
active_session_files: Dict[str, List[str]] = {}
active_session_images: Dict[str, List[str]] = {}
active_session_source_ids: Dict[str, str] = {}

file_lock = asyncio.Lock()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tiff"}

URL_IMAGE_PATTERN = re.compile(
    r'https?://[^\s]+\.(?:jpg|jpeg|png|gif|webp|bmp|svg|tiff)(?:\?[^\s]*)?',
    re.IGNORECASE
)

JSON_FILENAME_PATTERN = re.compile(r'([\w\-]+\.json)', re.IGNORECASE)

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://mcp-server:8000/mcp")
MCP_HTTP_BASE = MCP_SERVER_URL[:-4] if MCP_SERVER_URL.endswith("/mcp") else MCP_SERVER_URL

SHARED_UPLOAD_DIR = os.environ.get("SHARED_UPLOAD_DIR", "/app/sources")
SHARED_IMAGE_DIR = os.environ.get("SHARED_IMAGE_DIR", "/app/images")

os.makedirs(SHARED_UPLOAD_DIR, exist_ok=True)
os.makedirs(SHARED_IMAGE_DIR, exist_ok=True)


# ============================================================
# SESIONES
# ============================================================

def load_sessions() -> dict:
    """Recupera el historial de conversaciones."""
    if not os.path.exists(SESSIONS_FILE):
        return {}

    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[WARN] No se pudieron cargar las sesiones: {exc}")
        return {}


async def save_sessions_async():
    """Guarda las sesiones evitando escrituras simultáneas."""
    async with file_lock:
        async with aiofiles.open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(sessions_db, ensure_ascii=False, indent=2))


def extraer_archivos_json(texto: str) -> List[str]:
    """Detecta archivos JSON mencionados en la respuesta del agente."""
    if not texto:
        return []
    return list(dict.fromkeys(JSON_FILENAME_PATTERN.findall(texto)))


async def close_agent_session(session_id: str):
    """Cierra correctamente el agente y su conexión MCP."""
    agent = active_session_agents.pop(session_id, None)

    if not agent:
        return

    try:
        await agent.__aexit__(None, None, None)
    except Exception as exc:
        print(f"[WARN] Error cerrando agente {session_id}: {exc}")


async def get_session_agent(session_id: str):
    """Obtiene el agente de una sesión o crea uno nuevo."""
    if session_id in active_session_agents:
        return active_session_agents[session_id]

    agent = build_agent()
    await agent.__aenter__()
    active_session_agents[session_id] = agent

    return agent


async def guardar_interaccion(session_id: str, user_message: str, assistant_message: str):
    """Guarda una interacción usuario/agente."""
    session_data = sessions_db[session_id]

    session_data["messages"].append({
        "role": "user",
        "content": user_message
    })

    session_data["messages"].append({
        "role": "assistant",
        "content": assistant_message
    })

    await save_sessions_async()


# ============================================================
# LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global sessions_db

    sessions_db = load_sessions()
    print(f"[INFO] Sesiones cargadas: {len(sessions_db)}")

    yield

    await save_sessions_async()

    for session_id in list(active_session_agents.keys()):
        await close_agent_session(session_id)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="JUB Agent with MCP Integration",
    description="Backend conversacional del Agente JUB con Ollama + MCP + API JUB.",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)


# ============================================================
# MODELOS
# ============================================================

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


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "jub-agent",
        "mcp_server": MCP_SERVER_URL
    }


# ============================================================
# SESIONES
# ============================================================

@app.get("/sessions", response_model=List[SessionSummary])
async def list_sessions():
    return [
        {"id": sid, "title": data.get("title", "Nueva conversación")}
        for sid, data in sessions_db.items()
    ]


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

    return {
        "status": "deleted",
        "session_id": session_id
    }


# ============================================================
# ARCHIVOS MCP
# ============================================================

async def listar_archivos_mcp(source_id: Optional[str] = None) -> List[dict]:
    """Lista los archivos generados disponibles en MCP."""
    url = f"{MCP_HTTP_BASE}/files"
    params = {"source_id": source_id} if source_id else None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, params=params)

        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            return []

        files = data.get("files", [])
        return files if isinstance(files, list) else []

    except Exception as exc:
        print(f"[WARN] No se pudieron listar archivos MCP: {exc}")
        return []


@app.get("/download/{filename}")
async def descargar_archivo(filename: str):
    """Descarga exclusivamente archivos JSON generados por MCP."""

    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")

    if not filename.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="Solo se permite descargar archivos JSON.")

    url = f"{MCP_HTTP_BASE}/files/{filename}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)

    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo contactar al servidor MCP: {exc}"
        )

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail="No se pudo obtener el archivo."
        )

    return StreamingResponse(
        iter([response.content]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ============================================================
# GUARDADO DE ARCHIVOS
# ============================================================

async def guardar_upload(uploaded_file: UploadFile, directory: str) -> str:
    """Guarda un UploadFile evitando rutas inseguras."""
    filename = os.path.basename(uploaded_file.filename or "")

    if not filename:
        raise ValueError("El archivo no tiene un nombre válido.")

    file_path = os.path.join(directory, filename)

    async with aiofiles.open(file_path, "wb") as out_file:
        while True:
            chunk = await uploaded_file.read(1024 * 1024)

            if not chunk:
                break

            await out_file.write(chunk)

    return filename


async def descargar_imagen_url(url: str) -> Optional[str]:
    """Descarga una imagen externa y devuelve su nombre local."""
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url)

        if response.status_code != 200:
            print(f"[WARN] No se pudo descargar {url}. HTTP {response.status_code}")
            return None

        filename = os.path.basename(url.split("?")[0].split("/")[-1])
        extension = os.path.splitext(filename)[1].lower()

        if not filename or extension not in IMAGE_EXTENSIONS:
            filename = f"web_image_{uuid.uuid4().hex[:8]}.jpg"

        image_path = os.path.join(SHARED_IMAGE_DIR, filename)

        async with aiofiles.open(image_path, "wb") as out_file:
            await out_file.write(response.content)

        return filename

    except Exception as exc:
        print(f"[WARN] No fue posible descargar {url}: {exc}")
        return None


# ============================================================
# CHAT
# ============================================================

@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: Request,
    message: str = Form(""),
    file: Optional[List[UploadFile]] = File(None),
    session_id: Optional[str] = Form(None)
):
    message = (message or "").strip()

    if not message and not file:
        raise HTTPException(status_code=400, detail="Debes enviar un mensaje o un archivo.")

    # --------------------------------------------------------
    # CREAR O RECUPERAR SESIÓN
    # --------------------------------------------------------

    if not session_id or session_id not in sessions_db:
        session_id = str(uuid.uuid4())
        title = message[:30] + ("..." if len(message) > 30 else "") if message else "Nueva conversación"

        sessions_db[session_id] = {
            "title": title,
            "messages": []
        }

    session_data = sessions_db[session_id]
    base = str(request.base_url).rstrip("/")
    msg_lower = message.lower()

    # --------------------------------------------------------
    # AUDITORÍA LOCAL DE ARCHIVOS JSON
    #
    # Esta es la única intención que FastAPI resuelve
    # directamente porque pertenece a la interfaz y no
    # representa una consulta de negocio de JUB.
    # --------------------------------------------------------

    audit_keywords = [
        "archivos json",
        "json generados",
        "muéstrame los archivos",
        "muestrame los archivos",
        "archivos generados"
    ]

    if not file and any(keyword in msg_lower for keyword in audit_keywords):
        mcp_files = await listar_archivos_mcp()

        json_archivos = [
            item["name"]
            for item in mcp_files
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].lower().endswith(".json")
        ]

        downloads: List[DownloadItem] = []

        if json_archivos:
            respuesta_texto = "── Auditoría de Archivos JSON Generados ───────────────────\n\n"
            respuesta_texto += "Archivos JSON disponibles:\n\n"

            for nombre in json_archivos:
                respuesta_texto += f"- `{nombre}`\n"
                downloads.append(
                    DownloadItem(
                        name=nombre,
                        url=f"{base}/download/{nombre}"
                    )
                )
        else:
            respuesta_texto = (
                "── Auditoría de Archivos JSON Generados ───────────────────\n\n"
                "No existen archivos JSON generados actualmente."
            )

        await guardar_interaccion(session_id, message, respuesta_texto)

        return ChatResponse(
            session_id=session_id,
            title=session_data["title"],
            text=respuesta_texto,
            downloads=downloads or None
        )

    # --------------------------------------------------------
    # ARCHIVOS ADJUNTOS
    # --------------------------------------------------------

    file_info_list: List[str] = []

    if file:
        active_session_files.setdefault(session_id, [])
        active_session_images.setdefault(session_id, [])

        for uploaded_file in file:
            if not uploaded_file.filename:
                continue

            filename = os.path.basename(uploaded_file.filename)
            extension = os.path.splitext(filename)[1].lower()

            try:
                if extension in IMAGE_EXTENSIONS:
                    saved_name = await guardar_upload(uploaded_file, SHARED_IMAGE_DIR)

                    if saved_name not in active_session_images[session_id]:
                        active_session_images[session_id].append(saved_name)

                    file_info_list.append(
                        f"[Imagen adjunta disponible en '/app/images/{saved_name}'. "
                        "Si el usuario solicita analizarla, utiliza la herramienta MCP de visión.]"
                    )

                else:
                    saved_name = await guardar_upload(uploaded_file, SHARED_UPLOAD_DIR)

                    if saved_name not in active_session_files[session_id]:
                        active_session_files[session_id].append(saved_name)

                    file_info_list.append(
                        f"[Archivo de datos disponible en '/app/sources/{saved_name}'. "
                        f"Si una herramienta solicita csv_filename utiliza '{saved_name}'.]"
                    )

            except Exception as exc:
                file_info_list.append(
                    f"[No fue posible guardar el archivo '{filename}': {exc}]"
                )

    # --------------------------------------------------------
    # IMÁGENES MEDIANTE URL
    # --------------------------------------------------------

    detected_urls = URL_IMAGE_PATTERN.findall(message) if message else []

    if detected_urls:
        active_session_images.setdefault(session_id, [])

        for url_img in detected_urls:
            filename = await descargar_imagen_url(url_img)

            if not filename:
                file_info_list.append(
                    f"[No fue posible descargar la imagen externa: {url_img}]"
                )
                continue

            if filename not in active_session_images[session_id]:
                active_session_images[session_id].append(filename)

            file_info_list.append(
                f"[Imagen externa disponible en '/app/images/{filename}'. URL original: {url_img}]"
            )

    # --------------------------------------------------------
    # CONTEXTO DE ARCHIVOS DISPONIBLES
    # --------------------------------------------------------

    archivos_disponibles = [
        filename
        for filename in active_session_files.get(session_id, [])
        if os.path.exists(os.path.join(SHARED_UPLOAD_DIR, filename))
    ]

    imagenes_disponibles = [
        filename
        for filename in active_session_images.get(session_id, [])
        if os.path.exists(os.path.join(SHARED_IMAGE_DIR, filename))
    ]

    if archivos_disponibles:
        file_info_list.append(
            "Archivos de datos disponibles en esta conversación:\n"
            + "\n".join(f"- {filename}" for filename in archivos_disponibles)
        )

    if imagenes_disponibles:
        file_info_list.append(
            "Imágenes disponibles en esta conversación:\n"
            + "\n".join(f"- /app/images/{filename}" for filename in imagenes_disponibles)
        )

    # --------------------------------------------------------
    # PROMPT PARA EL AGENTE
    # --------------------------------------------------------

    prompt_parts: List[str] = []

    if file_info_list:
        prompt_parts.append("\n".join(file_info_list))

    prompt_parts.append(
        f"Solicitud actual del usuario:\n{message}"
        if message
        else "Solicitud actual del usuario:\nAnaliza los archivos adjuntos."
    )

    prompt_completo = "\n\n".join(prompt_parts)

    # --------------------------------------------------------
    # AGENTE JUB
    #
    # Usuario
    #   ↓
    # Ollama / Agente JUB
    #   ↓
    # Herramientas MCP
    #   ↓
    # API JUB
    #
    # Aquí NO se interceptan palabras como:
    # observatorio, producto, catálogo, datasource, etc.
    # El agente debe interpretar la intención.
    # --------------------------------------------------------

    try:
        agent = await get_session_agent(session_id)
        result = await agent.run(prompt_completo)
        respuesta_texto = getattr(result, "text", None) or str(result)

    except Exception as exc:
        print(f"[ERROR] Falló la ejecución del Agente JUB: {exc}")

        respuesta_texto = (
            "No fue posible completar la consulta mediante el Agente JUB.\n\n"
            f"Detalle técnico: {exc}"
        )

    # --------------------------------------------------------
    # DESCARGAS JSON MENCIONADAS POR EL AGENTE
    # --------------------------------------------------------

    nombres_json = extraer_archivos_json(respuesta_texto)

    downloads = [
        DownloadItem(
            name=nombre,
            url=f"{base}/download/{nombre}"
        )
        for nombre in nombres_json
    ]

    # --------------------------------------------------------
    # HISTORIAL
    # --------------------------------------------------------

    await guardar_interaccion(
        session_id=session_id,
        user_message=message,
        assistant_message=respuesta_texto
    )

    # --------------------------------------------------------
    # RESPUESTA
    # --------------------------------------------------------

    return ChatResponse(
        session_id=session_id,
        title=session_data["title"],
        text=respuesta_texto,
        downloads=downloads or None
    )