"""Construcción del agente (agent-framework) conectado al MCP del tutorial.

Mismo patrón que jub-agent/agent/jub_agent.py: un chat client (Ollama local)
+ `.as_agent(tools=MCPStreamableHTTPTool(...))`. Aquí, sin los
context_providers de RAG/CAG/Chroma de jub-agent — ese es un siguiente paso
sugerido en el notebook 05, no algo que este tutorial necesite para
enseñar MCP + agent-framework.
"""

import os

from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.ollama import OllamaChatClient

_INSTRUCTIONS = (
    "Eres un asistente que responde preguntas usando las herramientas MCP "
    "disponibles: una calculadora, un gestor de notas, y un catálogo de "
    "estaciones/sensores (clima, aire, agua). Usa siempre una tool cuando "
    "la pregunta lo requiera en vez de inventar la respuesta. Responde en "
    "español, de forma breve y directa."
)


def build_agent() -> Agent:
    chat_client = OllamaChatClient(
        host=os.environ.get("OLLAMA_URL", "http://ollama:11434"),
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b"),
    )
    return chat_client.as_agent(
        name="TutorAgent",
        instructions=_INSTRUCTIONS,
        tools=MCPStreamableHTTPTool(
            name="tutorial-mcp",
            url=os.environ.get("MCP_SERVER_URL", "http://mcp-server:8000/mcp"),
        ),
    )
