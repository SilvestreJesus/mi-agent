# JUB Agent — Model Context Protocol (MCP)

JUB Agent es un agente de Inteligencia Artificial desarrollado para automatizar la **preparación, transformación e indexación de datos en JUB** mediante **Model Context Protocol (MCP)**.

El sistema permite recibir instrucciones en lenguaje natural y archivos CSV para ejecutar operaciones sobre JUB, como la creación de observatorios, generación de catálogos, creación de productos, registro de DataSources e ingesta de DataRecords.

El proyecto integra **Microsoft Agent Framework**, **FastAPI**, **FastMCP**, **Ollama**, **Docker** y la **API REST v2 de JUB**.

---

## Arquitectura

La arquitectura separa la interpretación realizada por el agente de las operaciones deterministas necesarias para trabajar con JUB.

```text
┌─────────────────────────┐
│         Usuario         │
└────────────┬────────────┘
             │
             │ HTTP
             ▼
┌─────────────────────────┐
│     JUB Agent UI        │
│     Nginx :4000         │
└────────────┬────────────┘
             │
             │ HTTP
             ▼
┌────────────────────────────────────────────┐
│                  Agent                     │
│        FastAPI + Agent Framework           │
│                  :8091                     │
└──────────────┬────────────────┬────────────┘
               │                │
               │ Inferencia     │ MCP Streamable HTTP
               ▼                ▼
     ┌─────────────────┐   ┌─────────────────────────┐
     │     Ollama      │   │       MCP Server        │
     │     :11434      │   │       FastMCP :8000     │
     │                 │   │                         │
     │ LLM             │   │ Tools                   │
     │ Vision Model    │   │ - crear_observatorio   │
     └─────────────────┘   │ - crear_catalogos      │
                           │ - crear_productos      │
                           │ - datasource/ingesta   │
                           │ - análisis de imagen   │
                           │ - pipeline             │
                           └────────────┬────────────┘
                                        │
                                        │ HTTP / REST
                                        ▼
                           ┌─────────────────────────┐
                           │       JUB API v2        │
                           │                         │
                           │ Observatories           │
                           │ Catalogs                │
                           │ Products                │
                           │ DataSources             │
                           │ DataRecords             │
                           └─────────────────────────┘
```

El **Agent** interpreta las solicitudes y determina qué herramienta debe ejecutarse. El **MCP Server** contiene las herramientas que implementan las operaciones y utiliza la **API REST v2 de JUB** para registrar los recursos resultantes.

---

## Model Context Protocol

**Model Context Protocol (MCP)** es el protocolo utilizado para conectar el agente con las herramientas del proyecto.

Microsoft Agent Framework actúa como cliente MCP y FastMCP proporciona el servidor de herramientas.

```text
┌────────────────────────────┐
│ Microsoft Agent Framework  │
└─────────────┬──────────────┘
              │
              │ MCP Streamable HTTP
              ▼
┌────────────────────────────┐
│          FastMCP           │
├────────────────────────────┤
│ crear_observatorio         │
│ crear_catalogos            │
│ crear_productos            │
│ crear_datasource_y_ingestar│
│ analizar_imagen_con_ia     │
│ pipeline                   │
└─────────────┬──────────────┘
              │
              │ HTTP / REST
              ▼
┌────────────────────────────┐
│         JUB API v2         │
└────────────────────────────┘
```

Cada herramienta define una operación concreta, sus parámetros y la lógica necesaria para ejecutarla.

MCP no sustituye la API REST de JUB. MCP proporciona la interfaz entre el agente y las herramientas, mientras que las herramientas utilizan la API de JUB para ejecutar las operaciones de creación e indexación.

---

## Funcionamiento del agente

El agente está construido con **Microsoft Agent Framework** y utiliza modelos ejecutados localmente mediante **Ollama**.

Cuando el usuario realiza una solicitud, el flujo general es:

1. El frontend envía la solicitud al servicio `agent`.
2. FastAPI recibe el mensaje y, cuando corresponde, el archivo asociado.
3. Agent Framework envía el contexto al modelo ejecutado en Ollama.
4. El modelo interpreta la intención del usuario.
5. El agente selecciona una herramienta disponible en MCP.
6. FastMCP ejecuta la herramienta seleccionada.
7. La herramienta realiza las operaciones necesarias sobre JUB.
8. El resultado regresa al agente y posteriormente al usuario.

Por ejemplo:

```text
"Indexa este archivo CSV en JUB"
                │
                ▼
      ┌───────────────────┐
      │  Agent Framework  │
      │     + Ollama      │
      └─────────┬─────────┘
                │
                │ Tool selection
                ▼
      ┌───────────────────┐
      │    pipeline()     │
      │       MCP         │
      └─────────┬─────────┘
                │
                ▼
      ┌───────────────────┐
      │ Procesamiento CSV │
      ├───────────────────┤
      │ Observatory       │
      │ Catalogs          │
      │ Products          │
      │ DataSource        │
      │ DataRecords       │
      └─────────┬─────────┘
                │
                ▼
           JUB API v2
```

El modelo de lenguaje se utiliza para interpretar y seleccionar operaciones. La transformación e indexación se realiza mediante código determinista dentro de las herramientas MCP.

---

## Herramientas MCP

### `crear_observatorio`

Crea un nuevo Observatory en JUB a partir de información como:

- título;
- descripción;
- institución;
- edición;
- país;
- imagen asociada;
- usuario.

La herramienta realiza la creación mediante:

```text
POST /api/v2/observatories/setup
```

JUB devuelve principalmente:

```text
observatory_id
task_id
```

El `observatory_id` identifica el observatorio y se utiliza posteriormente para registrar sus catálogos y productos.

La tarea asociada al observatorio se completa para finalizar su aprovisionamiento y permitir que aparezca activo en JUB.

---

### `crear_catalogos`

Genera los catálogos utilizados para estructurar los datos del observatorio.

Actualmente la herramienta genera dos dimensiones principales:

| Catálogo | Función |
|---|---|
| `SPATIAL` | Representa la dimensión geográfica |
| `TEMPORAL` | Representa la dimensión temporal |

Para `SPATIAL`, el sistema busca información en columnas como:

```text
municipio
Municipio
estado
```

Para `TEMPORAL`, utiliza principalmente:

```text
anio
año
```

Conceptualmente:

```text
CSV
 │
 ├── municipio / estado ──────► SPATIAL
 │
 └── anio / año ──────────────► TEMPORAL
```

Los catálogos son registrados mediante una operación bulk:

```text
POST /api/v2/observatories/{observatory_id}/catalogs/bulk
```

---

### `crear_productos`

Genera Products asociados al Observatory.

Se crea un producto principal que representa el dataset completo y productos adicionales para cada año dentro del intervalo indicado.

Ejemplo para un periodo 2004-2014:

```text
Observatory
 │
 └── Products
      ├── Dataset completo 2004-2014
      ├── Product 2004
      ├── Product 2005
      ├── Product 2006
      ├── ...
      └── Product 2014
```

Los productos se registran mediante:

```text
POST /api/v2/observatories/{observatory_id}/products/bulk
```

---

### `crear_datasource_y_ingestar`

Crea un DataSource y transforma las filas del archivo CSV en DataRecords compatibles con JUB.

El procesamiento analiza dinámicamente las columnas del CSV.

Las columnas numéricas se incorporan en:

```text
numerical_interest_ids
```

mientras que los valores de texto pueden incorporarse en:

```text
interest_ids
```

Cada fila se transforma a una estructura equivalente a:

```json
{
  "record_id": "dataset-1",
  "spatial_id": "VICTORIA",
  "temporal_id": "2024-01-01T00:00:00Z",
  "interest_ids": [],
  "numerical_interest_ids": {
    "VARIABLE": 10.5
  },
  "raw_payload": {}
}
```

El DataSource se registra mediante:

```text
POST /api/v2/datasources
```

y los DataRecords mediante:

```text
POST /api/v2/datasources/{source_id}/records
```

Los registros son enviados en lotes de hasta **1000 DataRecords** para evitar realizar una petición HTTP por cada fila del CSV.

---

### `analizar_imagen_con_ia`

Permite analizar una imagen mediante un modelo multimodal ejecutado en Ollama.

La herramienta:

1. obtiene la imagen desde una URL;
2. descarga el contenido;
3. codifica la imagen en Base64;
4. envía la imagen y la instrucción al modelo multimodal;
5. devuelve el análisis generado.

El modelo de visión se configura mediante:

```env
OLLAMA_VISION_MODEL=llava
```

Esta capacidad permite analizar gráficas, mapas, diagramas y otros recursos visuales.

---

### `pipeline`

Ejecuta el proceso completo de indexación utilizando una sola herramienta.

El pipeline integra las principales etapas:

```text
┌─────────────────────────┐
│           CSV           │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│      Validación de      │
│       parámetros        │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│       Observatory       │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│        Catalogs         │
│   SPATIAL · TEMPORAL    │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│        Products         │
│ Dataset + Products/año  │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│        DataSource       │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ CSV → DataRecords       │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│  Ingesta por lotes      │
│  hasta 1000 registros   │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Finalización de tarea   │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Observatory activo JUB  │
└─────────────────────────┘
```

El pipeline permite realizar la indexación completa sin ejecutar manualmente cada herramienta.

Las herramientas individuales continúan disponibles cuando se requiere ejecutar o verificar una etapa de manera independiente.

---

## Contenedores Docker

El proyecto se ejecuta mediante **Docker Compose** y utiliza cuatro servicios principales.

| Servicio | Puerto | Descripción |
|---|---:|---|
| `jub-agent-frontend` | `4000` | Interfaz web del agente |
| `agent` | `8091` | API FastAPI y Agent Framework |
| `mcp-server` | `8000` | Servidor FastMCP |
| `ollama` | `11434` | Modelos de lenguaje y visión |

Los servicios se comunican mediante la red Docker:

```text
tutorial-net
```

### Puertos

#### Frontend

```text
http://localhost:4000
```

Interfaz web utilizada para interactuar con JUB Agent.

#### Agent API

```text
http://localhost:8091
```

API FastAPI utilizada por la interfaz para enviar mensajes y archivos al agente.

El servicio ejecuta internamente la aplicación en el puerto `8081`.

#### MCP Server

```text
http://localhost:8000
```

Puerto utilizado por el servidor FastMCP.

Dentro de Docker, el agente utiliza:

```text
http://mcp-server:8000/mcp
```

para establecer la comunicación mediante MCP Streamable HTTP.

#### Ollama

```text
http://localhost:11434
```

Puerto utilizado por Ollama para proporcionar acceso a los modelos.

Dentro de Docker:

```text
http://ollama:11434
```

> En la configuración actual de Docker Compose, los puertos `8000` de MCP y `11434` de Ollama se utilizan internamente entre contenedores. Para acceder a ellos directamente mediante `localhost`, es necesario publicar esos puertos en Docker Compose.

---

## Comunicación entre servicios

La comunicación entre los contenedores puede resumirse de la siguiente forma:

```text
                    http://localhost:4000
                             │
                             ▼
                ┌──────────────────────┐
                │  jub-agent-frontend  │
                │        Nginx         │
                └──────────┬───────────┘
                           │
                           │ HTTP
                           ▼
                    localhost:8091
                           │
                ┌──────────▼───────────┐
                │        agent         │
                │ FastAPI              │
                │ Agent Framework      │
                └──────┬────────┬──────┘
                       │        │
              Ollama   │        │ MCP
                       │        │
                       ▼        ▼
              ┌────────────┐  ┌──────────────┐
              │   ollama   │  │  mcp-server  │
              │   :11434   │  │    :8000     │
              └────────────┘  └──────┬───────┘
                                     │
                                     │ HTTP / REST
                                     ▼
                              ┌──────────────┐
                              │  JUB API v2  │
                              └──────────────┘
```

---

## Estructura del proyecto

```text
project/
├── agent/
│   ├── main.py
│   └── tutor_agent.py
│
├── mcp_server/
│   ├── server.py
│   ├── config.py
│   ├── tools/
│   ├── sources/
│   ├── data/
│   └── images/
│
├── index.html
├── styles.css
├── .env
├── .env.example
├── docker-compose.yml
└── README.md
```

### `agent/`

Contiene la API FastAPI y la configuración de Microsoft Agent Framework.

Se encarga de recibir solicitudes, administrar sesiones y archivos, comunicarse con Ollama y ejecutar las herramientas disponibles mediante MCP.

### `mcp_server/`

Contiene el servidor FastMCP y la implementación de las herramientas encargadas de realizar las operaciones sobre JUB.

### `mcp_server/sources/`

Almacena los archivos CSV utilizados como entrada durante los procesos de indexación.

### `mcp_server/data/`

Contiene archivos y estados generados durante el procesamiento de datos.

### `mcp_server/images/`

Almacena recursos utilizados por las herramientas de procesamiento multimodal.

---

## Configuración

La configuración principal se establece mediante variables de entorno en `.env`.

Ejemplo:

```env
JUB_API_URL=http://localhost:5000
JUB_USERNAME=invitado
JUB_PASSWORD=invitado

OLLAMA_MODEL=qwen2.5:1.5b
OLLAMA_VISION_MODEL=llava

MCP_PORT=8000
```

Docker Compose configura las direcciones internas utilizadas por los servicios:

```env
OLLAMA_URL=http://ollama:11434
MCP_SERVER_URL=http://mcp-server:8000/mcp
```

El servidor MCP también recibe:

```env
JUB_API_URL=${JUB_API_URL}
```

para comunicarse con la instancia configurada de JUB.

---

## Volúmenes

El proyecto utiliza volúmenes para compartir archivos entre los servicios.

```text
./mcp_server/sources  → /app/sources
./mcp_server/data     → /app/data
./mcp_server/images   → /app/images
```

El contenedor `agent` comparte `sources` e `images` con `mcp-server`, permitiendo que los archivos cargados desde la interfaz puedan ser utilizados posteriormente por las herramientas MCP.

Ollama utiliza:

```text
ollama_data:/root/.ollama
```

para mantener los modelos descargados entre ejecuciones del contenedor.

---

## Ejecución

Construir e iniciar todos los servicios:

```bash
docker compose up --build
```

Ejecutar en segundo plano:

```bash
docker compose up -d --build
```

Verificar el estado:

```bash
docker compose ps
```

Consultar todos los logs:

```bash
docker compose logs -f
```

Consultar únicamente el agente:

```bash
docker compose logs -f agent
```

Consultar MCP:

```bash
docker compose logs -f mcp-server
```

Consultar Ollama:

```bash
docker compose logs -f ollama
```

Detener los servicios:

```bash
docker compose down
```

---

## Acceso a los servicios

Después de iniciar Docker Compose:

| Componente | Dirección |
|---|---|
| JUB Agent UI | `http://localhost:4000` |
| Agent API | `http://localhost:8091` |
| MCP Server | `http://localhost:8000`* |
| Ollama API | `http://localhost:11434`* |
| JUB API | definida mediante `JUB_API_URL` |

\* MCP Server y Ollama funcionan actualmente como servicios internos de Docker. Para acceder a ellos directamente desde el host mediante `localhost`, sus puertos deben publicarse en `docker-compose.yml`.

---

## Tecnologías utilizadas

| Tecnología | Función |
|---|---|
| Python | Implementación del agente y herramientas |
| FastAPI | API HTTP del agente |
| Microsoft Agent Framework | Construcción y ejecución del agente |
| Model Context Protocol | Comunicación entre agente y herramientas |
| FastMCP | Implementación del servidor MCP |
| Ollama | Ejecución local de modelos |
| LLaVA | Análisis multimodal |
| httpx | Comunicación con la API de JUB |
| Docker | Contenedorización |
| Docker Compose | Orquestación de servicios |
| Nginx | Servicio del frontend |
| JUB API v2 | Registro e indexación de recursos |

---

## Resumen del flujo

JUB Agent transforma una solicitud en lenguaje natural en operaciones estructuradas sobre JUB:

```text
┌──────────────────────────┐
│ Usuario + Dataset        │
└────────────┬─────────────┘
             ▼
┌──────────────────────────┐
│ JUB Agent                │
│ FastAPI + Agent Framework│
└────────────┬─────────────┘
             │
             ├──────────────► Ollama
             │                Interpretación
             │
             ▼
┌──────────────────────────┐
│ Model Context Protocol   │
└────────────┬─────────────┘
             ▼
┌──────────────────────────┐
│ FastMCP Tools            │
│                          │
│ Observatory              │
│ Catalogs                 │
│ Products                 │
│ DataSource               │
│ DataRecords              │
└────────────┬─────────────┘
             │
             │ REST
             ▼
┌──────────────────────────┐
│ JUB API v2               │
└────────────┬─────────────┘
             ▼
┌──────────────────────────┐
│ Observatory indexado     │
└──────────────────────────┘
```

La arquitectura mantiene separadas la **interpretación mediante el modelo de lenguaje**, la **orquestación mediante MCP**, la **ejecución de las herramientas** y la **persistencia de los recursos mediante JUB API v2**.