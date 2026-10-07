"""Servidor MCP del tutorial.

Mismo patrón que jub-agent/mcp/server.py: FastMCP + módulos de tools que
exponen un `register(mcp)`. Transporte streamable-http, para que el agente
(y cualquier cliente MCP) se conecte por HTTP en vez de stdio.
"""

import os

from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

# 1. Agrega 'pipeline' a las importaciones
from tools import basic, catalog, csv_converter, observatory, datasource, products, queries, service, pipeline 

mcp = FastMCP(
    name="tutorial-mcp",
    instructions=(
    "Servidor MCP para la gestión e indexación de datos en JUB. "
    "Proporciona herramientas para analizar y convertir archivos CSV, "
    "crear y gestionar observatorios, catálogos, productos y datasources, "
    "ingerir registros, consultar información de JUB y ejecutar el pipeline "
    "completo de indexación."
    ),
)

#basic.register(mcp)
#catalog.register(mcp)
csv_converter.register(mcp)
observatory.register(mcp)
datasource.register(mcp)
products.register(mcp)
queries.register(mcp)
service.register(mcp)

# 2. Registra el pipeline en el servidor MCP
pipeline.register(mcp)

if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("MCP_PORT", "8000")),
    )