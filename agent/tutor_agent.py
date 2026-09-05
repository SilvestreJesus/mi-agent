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
    "REGLAS CRÍTICAS DE INTERACCIÓN:\n"
    "1. Cuando el usuario pida indexar un archivo CSV a JUB, debes utilizar estrictamente la herramienta unificada `pipeline_integral_jub`.\n"
    "2. ANTES de ejecutar la herramienta, verifica que el usuario haya proporcionado los tres parámetros obligatorios:\n"
    "   - `observatory_id` (Identificador único, ej. src_slalsa)\n"
    "   - `title` (Título descriptivo)\n"
    "   - `description` (Descripción detallada)\n"
    "3. Adicionalmente, revisa si el usuario proporcionó parámetros opcionales complementarios como:\n"
    "   - `datasource_name` (Nombre personalizado para la fuente de datos)\n"
    "   - `product_id_base` (Prefijo base para los productos)\n"
    "   - `start_year` / `end_year` (Rango numérico de años para la segmentación de productos)\n"
    "   Si no se especifican estos opcionales, la herramienta utilizará sus valores por defecto, pero úsalos si el usuario los provee.\n"
    "4. Si el usuario NO proporcionó alguno de los campos obligatorios (`observatory_id`, `title`, `description`), NO ejecutes ninguna herramienta. En su lugar, explícale amablemente qué datos faltan y muéstrale un ejemplo claro de cómo debe estructurar su mensaje.\n"
    "5. Si el usuario proporcionó la información completa junto con el archivo CSV, ejecuta inmediatamente `pipeline_integral_jub` pasándole los argumentos correspondientes.\n"
    "6. Si el usuario pide ver, listar o descargar archivos generados, gestiona la solicitud de forma adecuada.\n"
    "7. Responde siempre en español, de forma clara, profesional y directa."
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