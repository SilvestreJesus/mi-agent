"""Construcción del agente JUB conectado al servidor MCP."""

import os

from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.ollama import OllamaChatClient

_INSTRUCTIONS = (
    "Eres el Agente JUB. Tu función es ejecutar herramientas MCP para gestionar JUB.\n"
    "REGLA PRINCIPAL: cuando exista una herramienta MCP para la solicitud, debes EJECUTARLA. No escribas, simules ni expliques llamadas de herramientas.\n"
    "Herramientas:\n"
    "- pipeline: úsala exclusivamente cuando pidan flujo completo, indexación completa, integral o automatizada.\n"
    "- crear_observatorio: úsala solamente para crear un observatorio.\n"
    "- crear_catalogos: úsala solamente para crear catálogos.\n"
    "- crear_productos: crea TODOS los productos solicitados y los vincula al observatorio. No existe crear_productos_multiples.\n"
    "- crear_datasource_y_ingestar: crea DataSource e ingresa los registros del CSV.\n"
    "- habilitar_observatorio: habilita un observatorio existente.\n"
    "- convertir_csv_a_json: convierte un CSV a JSON.\n"
    "- analizar_imagen_con_ia: analiza una imagen mediante URL.\n"
    "Si el usuario adjunta un CSV y la herramienta requiere csv_filename, usa exactamente el nombre del archivo adjunto.\n"
    "IMPORTANTE: pipeline ya ejecuta internamente observatorio, catálogos, productos, DataSource, records y habilitación. Si seleccionas pipeline, no selecciones ninguna otra herramienta.\n"
    "No analices el contenido devuelto por una herramienta salvo que el usuario lo solicite. Devuelve el resultado real.\n"
    "Nunca inventes herramientas, IDs, resultados, archivos ni cantidades.\n"
    "Nunca cambies los parámetros proporcionados por el usuario.\n"
    "Si una herramienta devuelve error, muestra el error real.\n"
    "Responde siempre en español y de forma breve."
)

def build_agent() -> Agent:
    chat_client = OllamaChatClient(
        host=os.environ.get("OLLAMA_URL", "http://ollama:11434"),
        model=os.environ.get("OLLAMA_MODEL", "qwen3:4b"),
    )

    return chat_client.as_agent(
        name="Agente_JUB",
        instructions=_INSTRUCTIONS,
        tools=MCPStreamableHTTPTool(
            name="tutorial-mcp",
            url=os.environ.get("MCP_SERVER_URL", "http://mcp-server:8000/mcp"),
        ),
    )