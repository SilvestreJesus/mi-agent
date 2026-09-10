FROM python:3.12-slim
WORKDIR /app

# Agregamos aiofiles a esta línea:
RUN pip install --no-cache-dir "fastapi" "uvicorn[standard]" python-dotenv aiofiles

# agent-framework y su soporte MCP siguen en preview (--pre), igual que en
# jub-agent/agent/Dockerfile. Dos pines explícitos necesarios:
#  - "httpx<1": --pre deja que el resolver elija versiones dev de httpx
#    (ej. 1.0.dev3), que rompen al paquete `ollama` (cambia la firma de
#    `Client.__init__`, ya no acepta `base_url`).
#  - "mcp==1.28.1": mismo pin que usa jub-agent/agent/Dockerfile. La versión
#    2.x del SDK `mcp` renombró `InitializeResult.protocolVersion` a
#    `protocol_version`, y agent-framework 1.14.x todavía accede al nombre
#    viejo — sin este pin, el agente truena al conectar con el servidor MCP.
RUN pip install --no-cache-dir --pre agent-framework-ollama "mcp==1.28.1" "httpx<1"

COPY . .

EXPOSE 8081
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081"]