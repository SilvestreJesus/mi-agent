import json
from pathlib import Path
from typing import Optional

import httpx
from fastmcp import FastMCP

from config import DATA_RECORDS_FILE, JUB_PASS, JUB_URL, JUB_USER, STATE_FILE


SOURCES_DIR = Path("sources")
IMAGES_DIR = Path("images")
DATA_DIR = Path("data")


def resolve_existing_path(filename: str) -> Path:
    """Busca un archivo dando prioridad a fuentes locales."""
    candidate = Path(filename)
    if candidate.exists():
        return candidate

    for folder in [
        DATA_DIR,
        SOURCES_DIR,
        Path("/app/data"),
        Path("/app/sources"),
        IMAGES_DIR,
        Path("/app/images"),
        Path("/app"),
    ]:
        alt = folder / candidate.name
        if alt.exists():
            return alt

    return candidate


def register(mcp: FastMCP):
    """Registra la herramienta datasource en el servidor FastMCP."""

    @mcp.tool(name="ingresar_datasource")
    async def ingresar_datasource(
        datasource_name: str,
        datasource_description: str,
        connection_uri: Optional[str] = None,
        format_type: str = "csv",
        test_dsl_query: str = "jub.v1.VI(VALOR)",
        records_filename: Optional[str] = None,
    ) -> str:
        """Crea el DataSource, lo enlaza al Observatorio, mapea registros con el índice e ingesta masivamente."""
        
        # 1. Determinar y resolver la ruta de registros
        records_path = (
            resolve_existing_path(records_filename)
            if records_filename
            else DATA_RECORDS_FILE
        )

        if not STATE_FILE.exists() or not records_path.exists():
            return f"Error: No se encontró {STATE_FILE} o {records_path}."

        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)

        with open(records_path, encoding="utf-8") as f:
            records = json.load(f)

        observatory_id = state.get("observatory_id")
        item_index = state.get("item_index", {})

        # Si no se provee connection_uri, lo inferimos de forma dinámica
        if not connection_uri:
            csv_original = state.get("csv_filename", f"{records_path.stem.replace('data_records_', '')}.csv")
            connection_uri = f"file://data/{Path(csv_original).name}"

        async with httpx.AsyncClient(
            base_url=JUB_URL, timeout=60.0
        ) as client:
            headers = {}

            # Auth
            try:
                auth_res = await client.post(
                    "/api/v2/users/auth",
                    json={"username": JUB_USER, "password": JUB_PASS},
                )
                if auth_res.status_code in (200, 201):
                    token = auth_res.json().get("access_token")
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
            except Exception as e:
                print(f"Auth error: {e}")

            # 1. Registrar DataSource 
            source_id = None
            ds_payload = {
                "name": datasource_name,
                "description": datasource_description,
                "format": format_type,
                "connection_uri": connection_uri,
            }
            
            ds_res = await client.post("/api/v2/datasources", json=ds_payload, headers=headers)
            
            if ds_res.status_code in (200, 201):
                source_id = ds_res.json().get("source_id") or ds_res.json().get("id")
            else:
                # Si falló (probablemente porque ya existe), lo buscamos en el listado
                list_res = await client.get("/api/v2/datasources", headers=headers)
                if list_res.status_code == 200:
                    for ds in list_res.json():
                        if ds.get("name") == datasource_name:
                            source_id = ds.get("source_id") or ds.get("id")
                            break

            if not source_id:
                return f"Error: No se pudo crear ni recuperar el DataSource. Respuesta del servidor: {ds_res.text}"

            # 2. Enlazar al Observatorio 
            if observatory_id:
                await client.post(
                    f"/api/v2/observatories/{observatory_id}/datasources",
                    json={"source_id": source_id},
                    headers=headers,
                )

            # 3. Mapear e Ingestar Registros
            for rec in records:
                rec["source_id"] = source_id
                if "spatial_id" in rec:
                    rec["spatial_id"] = item_index.get(rec["spatial_id"], rec["spatial_id"])
                    
                if "interest_ids" in rec and isinstance(rec["interest_ids"], list):
                    rec["interest_ids"] = [
                        item_index.get(iid, iid) for iid in rec["interest_ids"]
                    ]

            ingest_res = await client.post(
                f"/api/v2/datasources/{source_id}/records",
                json=records,
                headers=headers,
            )
            
            if ingest_res.status_code not in (200, 201):
                return f"Error en ingesta de registros ({ingest_res.status_code}): {ingest_res.text}"

            # 4. Consulta DSL de prueba
            query_res = await client.post(
                f"/api/v2/datasources/{source_id}/query",
                json={"query": test_dsl_query, "limit": 5, "skip": 0},
                headers=headers,
            )
            query_results = (
                query_res.json() if query_res.status_code == 200 else []
            )

            # 5. Actualizar estado (.state.json)
            state["source_id"] = source_id
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)

            return json.dumps(
                {
                    "status": "success",
                    "source_id": source_id,
                    "records_ingested": len(records),
                    "connection_uri_used": connection_uri,
                    "query_sample_results": (
                        query_results[:2]
                        if isinstance(query_results, list)
                        else query_results
                    ),
                },
                ensure_ascii=False,
                indent=2
            )