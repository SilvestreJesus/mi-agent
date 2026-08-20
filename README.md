# Tutorial: MCP + agent-framework

Tutorial autocontenido sobre **Model Context Protocol (MCP)** y **Microsoft
`agent-framework`**: qué son, cómo funcionan por dentro, y cómo construir un
agente que use tools MCP — con el mismo patrón que usa [`jub-agent`](../jub-agent),
pero sin depender de la API real de jub ni de credenciales (usa un catálogo
mock en memoria).

## Cómo está organizado

1. **`notebooks/`** — el tutorial explicativo, en orden:
   1. `01_que_es_mcp.ipynb` — qué es MCP, arquitectura, primitivas, un cliente MCP hecho a mano.
   2. `02_servidor_mcp_basico.ipynb` — construir un servidor MCP con tools genéricas (calculadora, notas).
   3. `03_agent_framework_basico.ipynb` — qué es `agent-framework`, un agente con tools Python planas (sin MCP todavía).
   4. `04_conectando_agente_a_mcp.ipynb` — conectar el agente del notebook 3 al servidor del notebook 2 vía MCP.
   5. `05_tools_estilo_jub.ipynb` — tools de consulta más ricas (estilo `jub-agent/mcp/tools/products.py`) sobre un catálogo mock, y mapeo explícito de vuelta al proyecto real.
2. **`mcp_server/`** y **`agent/`** — la versión "productizada" de los notebooks: mismo código, empaquetado y corriendo como servicios Docker de larga duración.

## Requisitos

- Python 3.12+ y [Ollama](https://ollama.com/) instalado localmente (para los notebooks).
- Docker + Docker Compose (para la parte empaquetada).
- Sin API keys: todo corre con un modelo pequeño de Ollama, igual que la ruta local de `jub-agent`.

## Correr los notebooks

```bash
ollama pull qwen2.5:1.5b   # o el modelo que prefieras
pip install jupyterlab fastmcp python-dotenv "httpx<1" "mcp==1.28.1" --pre agent-framework-ollama
jupyter lab notebooks/
```

> **Sobre los pines de versión:** al momento de escribir esto,
> `agent-framework-ollama` (`--pre`, sigue en preview) es incompatible con las
> versiones más nuevas de sus propias dependencias si no las fijas a mano:
> `mcp>=2.0` renombró un atributo interno que `agent-framework` todavía
> espera con el nombre viejo (falla al conectar al servidor MCP), y `--pre`
> deja que pip elija versiones *dev* de `httpx` (rompen al paquete
> `ollama`). Si en el futuro actualizas `agent-framework-ollama` y algo deja
> de funcionar, ese es el primer lugar donde mirar.

Ábrelos en orden (01 → 05); cada uno indica en su primera celda qué hay que
tener corriendo (por ejemplo, `ollama serve` en otra terminal).

## Correr la versión empaquetada (Docker)

```bash
cp .env.example .env
docker compose up --build
```

Esto levanta 3 servicios en la red `tutorial-net`:

| Servicio     | Puerto host | Rol                                                  |
|--------------|-------------|-------------------------------------------------------|
| `ollama`     | 11435       | LLM local (descarga el modelo en el primer arranque)  |
| `mcp-server` | 8090        | Servidor MCP (tools genéricas + catálogo mock)         |
| `agent`      | 8091        | Agente `agent-framework` conectado al MCP vía HTTP     |

(Puertos host no estándar a propósito, para no chocar con `jub-agent` u otros
stacks corriendo en la misma máquina — internamente cada servicio sigue
usando su puerto "normal": 8000, 8081, 11434.)

El primer arranque tarda unos minutos (descarga del modelo). Cuando los tres
healthchecks estén en verde:

> **Nota sobre el modelo por defecto:** `qwen2.5:1.5b` es deliberadamente
> pequeño (rápido de descargar, corre bien en CPU) para que el tutorial sea
> ágil. Es suficiente para disparar las tools correctamente, pero a veces
> comete errores de razonamiento en la respuesta final en texto (ej.
> confundir un conteo) aunque el resultado de la tool haya sido correcto.
> Si quieres respuestas más consistentes, sube `OLLAMA_MODEL` a algo más
> grande en tu `.env` (ej. `qwen3:4b`, como usa `jub-agent` por defecto).

```bash
curl http://localhost:8091/health

curl -X POST http://localhost:8091/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Crea una nota que diga hola mundo y luego lístala"}'

curl -X POST http://localhost:8091/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "¿Cuántos sensores de aire hay y cuál es el promedio?"}'
```

## Relación con `jub-agent`

Este tutorial reutiliza deliberadamente las mismas convenciones que
[`jub-agent`](../jub-agent):

| Aquí                                  | Equivalente real en `jub-agent`                    |
|----------------------------------------|-----------------------------------------------------|
| `mcp_server/server.py`                 | `jub-agent/mcp/server.py`                            |
| `mcp_server/tools/*.py` (`register`)   | `jub-agent/mcp/tools/*.py`                           |
| `agent/tutor_agent.py::build_agent()`  | `jub-agent/agent/jub_agent.py::build_agent()`        |
| `agent/main.py`                        | `jub-agent/agent/main.py` (sin sesiones/RAG/Chroma)  |
| `docker-compose.yml` + Dockerfiles     | Mismos, simplificados (sin ChromaDB, sin UI Chainlit)|

Lo que **no** está aquí a propósito, y que puedes ir a ver directamente en
`jub-agent` como "siguiente paso": RAG/CAG contra ChromaDB
(`agent/providers/`), selección multi-proveedor de LLM (NVIDIA/Docker Model
Runner/Ollama), sesiones persistentes por usuario, y una UI (Chainlit) sobre
el agente.
