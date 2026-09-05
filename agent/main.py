import os
import json
import re
import uuid
from contextlib import asynccontextmanager
from typing import List, Optional

import httpx
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from tutor_agent import build_agent

# Estructura para almacenar sesiones en memoria y persistencia simple en disco
SESSIONS_FILE = "sessions_history.json"
sessions_db = {}

def load_sessions():
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_sessions():
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions_db, f, ensure_ascii=False, indent=2)

sessions_db = load_sessions()
active_session_agents = {}
active_session_files = {}
active_session_source_ids = {}

# ---------- Config del servidor MCP (Puerto 8091 / agente) ----------
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://mcp-server:8000/mcp")
MCP_HTTP_BASE = MCP_SERVER_URL[: -len("/mcp")] if MCP_SERVER_URL.endswith("/mcp") else MCP_SERVER_URL

# ---------- Config del servidor JUB (Puerto 5000 / Indexación) ----------
JUB_API_URL = "http://host.docker.internal:5000"

JSON_FILENAME_PATTERN = re.compile(r'([\w\-]+\.json)')
SOURCE_ID_PATTERN = re.compile(r'\bsrc_[a-z0-9_]+\b', re.IGNORECASE)

ARCHIVOS_INTENT_PATTERN = re.compile(
    r'\b(json|archivo|archivos|descarg\w*|catalogo|catálogo|catalogos|catálogos|'
    r'data_record|data_records|registro generado|registros generados)\b',
    re.IGNORECASE,
)


def extraer_archivos_json(texto: str) -> List[str]:
    nombres = JSON_FILENAME_PATTERN.findall(texto)
    vistos: List[str] = []
    for n in nombres:
        if n not in vistos:
            vistos.append(n)
    return vistos


def parece_pedido_de_archivos(mensaje: str) -> bool:
    return bool(ARCHIVOS_INTENT_PATTERN.search(mensaje))


def extraer_source_id_mencionado(mensaje: str) -> Optional[str]:
    m = SOURCE_ID_PATTERN.search(mensaje)
    return m.group(0) if m else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global sessions_db
    sessions_db = load_sessions()
    yield
    save_sessions()

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
        save_sessions()
    if session_id in active_session_agents:
        del active_session_agents[session_id]
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



async def indexar_en_jub(endpoint_path: str, payload: dict = None) -> dict:
    url = f"{JUB_API_URL}{endpoint_path}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload)
    resp.raise_for_status()
    return resp.json()


@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: Request,
    message: str = Form(...),
    file: Optional[UploadFile] = File(None),
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


    if not file and parece_pedido_de_archivos(message):
        source_id_mencionado = extraer_source_id_mencionado(message) or active_session_source_ids.get(session_id)

        try:
            archivos = await listar_archivos_mcp(source_id_mencionado)
        except Exception:
            archivos = None

        if archivos is not None:
            if archivos:
                nombres = [a["name"] for a in archivos]
                respuesta_texto = (
                    f"Encontré {len(nombres)} archivo(s) JSON"
                    + (f" para '{source_id_mencionado}'" if source_id_mencionado else "")
                    + ":\n" + "\n".join(f"- {n}" for n in nombres)
                )
                downloads = [DownloadItem(name=n, url=f"{base}/download/{n}") for n in nombres]
            else:
                respuesta_texto = (
                    "No encontré archivos JSON generados todavía"
                    + (f" para '{source_id_mencionado}'" if source_id_mencionado else "")
                    + ". Primero sube y convierte un archivo CSV."
                )
                downloads = None

            session_data["messages"].append({"role": "user", "content": message})
            session_data["messages"].append({"role": "assistant", "content": respuesta_texto})
            save_sessions()

            return ChatResponse(
                session_id=session_id,
                title=session_data["title"],
                text=respuesta_texto,
                downloads=downloads,
            )

    if session_id not in active_session_agents:
        agent = build_agent()
        await agent.__aenter__()
        active_session_agents[session_id] = agent

    agent = active_session_agents[session_id]

    file_info = ""
    if file:
        contents = await file.read()
        file_path = os.path.abspath(f"temp_{file.filename}")
        with open(file_path, "wb") as f:
            f.write(contents)
        active_session_files[session_id] = file_path
        file_info = f"\n[Ruta absoluta del archivo CSV adjunto en esta sesión: {file_path}, Nombre del archivo: {file.filename}]"
    elif session_id in active_session_files:
        stored_path = active_session_files[session_id]
        if os.path.exists(stored_path):
            file_info = f"\n[Ruta absoluta del archivo CSV previamente adjunto: {stored_path}]"

    prompt_completo = message + file_info


    result = await agent.run(prompt_completo)
    respuesta_texto = getattr(result, "text", str(result))

    m = SOURCE_ID_PATTERN.search(respuesta_texto)
    if m:
        active_session_source_ids[session_id] = m.group(0)

    nombres_json = extraer_archivos_json(respuesta_texto)
    downloads = None
    if nombres_json:
        downloads = [DownloadItem(name=n, url=f"{base}/download/{n}") for n in nombres_json]

    session_data["messages"].append({"role": "user", "content": message})
    session_data["messages"].append({"role": "assistant", "content": respuesta_texto})
    save_sessions()

    return ChatResponse(
        session_id=session_id,
        title=session_data["title"],
        text=respuesta_texto,
        downloads=downloads
    )