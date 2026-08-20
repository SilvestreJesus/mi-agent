"""API mínima (FastAPI) que expone el agente del tutorial por HTTP.

Mismo espíritu que jub-agent/agent/main.py (build del agente en el
`lifespan`, endpoint /health) pero simplificado: sin sesiones persistentes,
sin streaming SSE, sin Chroma/RAG. El foco es mostrar el ciclo completo
"HTTP -> agent-framework -> MCP -> tool -> LLM -> respuesta" de la forma
más corta posible.
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from tutor_agent import build_agent

_agent = None
_ready = asyncio.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    _agent = build_agent()
    await _agent.__aenter__()
    _ready.set()
    yield
    await _agent.__aexit__(None, None, None)


app = FastAPI(title="Tutorial Agent", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    text: str


@app.get("/health")
async def health():
    return {"status": "ok", "ready": _ready.is_set()}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    await asyncio.wait_for(_ready.wait(), timeout=120.0)
    result = await _agent.run(req.message)
    return ChatResponse(text=result.text)
