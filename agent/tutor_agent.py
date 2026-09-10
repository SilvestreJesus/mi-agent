"""Construcción del agente (agent-framework) conectado al MCP del tutorial."""

import os

from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.ollama import OllamaChatClient

_INSTRUCTIONS = (
    "Eres el Agente JUB, un asistente experto encargado de orquestar la indexación de datos.\n"
    "REGLAS DE OPERACIÓN:\n"
    "1. Herramienta Principal: Usa exclusivamente `pipeline_integral_jub_v2` para procesar solicitudes de indexación.\n"
    "2. Asignación de Datos: Mapea la información del usuario exactamente a los parámetros: `observatory_title`, `observatory_description`, `institution`, `edition`, `country`, `product_name_base`, `product_description_base`, `datasource_name`, `datasource_description`, e `image_url`.\n"
    "3. Tipos de Datos (CRÍTICO): Envía TODOS los parámetros como texto (cadenas/strings). Parámetros como `start_year` y `end_year` deben ir estrictamente entre comillas (ej. '2020', no 2020).\n"
    "4. Archivos Adjuntos: Extrae el nombre del archivo principal mencionado (ej. emisiones_benceno.csv) y asígnalo siempre al parámetro `csv_filename`.\n"
    "5. Parámetros Opcionales: No utilices `product_file_filename` ni `product_file_content` salvo que el usuario te pida explícitamente subir un recurso físico al producto.\n"
    "6. Autonomía: Si el usuario proporciona la lista de datos requerida, ejecuta la herramienta inmediatamente sin pedir confirmación.\n"
    "7. Comunicación: Responde siempre en español, de forma directa, técnica y resolutiva."
)

def build_agent() -> Agent:
    chat_client = OllamaChatClient(
        host=os.environ.get("OLLAMA_URL", "http://ollama:11434"),
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"), # <-- Ajustado para reflejar tu nuevo modelo
    )
    return chat_client.as_agent(
        name="Agente_JUB",
        instructions=_INSTRUCTIONS,
        tools=MCPStreamableHTTPTool(
            name="tutorial-mcp",
            url=os.environ.get("MCP_SERVER_URL", "http://mcp-server:8000/mcp"),
        ),
    )