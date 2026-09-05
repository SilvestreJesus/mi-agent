import json
import os
from pathlib import Path
from typing import Any, Dict, List

import httpx
from fastmcp import FastMCP

JUB_URL = os.environ.get("JUB_API_URL", "http://localhost:5000")
JUB_USER = os.environ.get("JUB_USERNAME", "invitado")
JUB_PASS = os.environ.get("JUB_PASSWORD", "invitado")

DATA_RECORDS_FILE = Path("data/data_records.json")  
STATE_FILE = Path(".state.json")

def register(mcp: FastMCP):
    """Registra la herramienta de ingesta de DataSources y consultas en el servidor FastMCP."""

    @mcp.tool(name="ingresar_datasource")
    async def ingresar_datasource(
        datasource_name: str,
        datasource_description: str,
        connection_uri: str = "file://data/emisiones_benceno.csv",
        format_type: str = "csv"
    ) -> str:
        """
        Registra o recupera un DataSource en JUB de forma única, mapea e ingesta registros masivos
        usando el índice guardado en .state.json, y ejecuta una consulta de prueba con DSL.
        """
        if not STATE_FILE.exists() or not DATA_RECORDS_FILE.exists():
            return f"Error: No se encontró el archivo de estado ({STATE_FILE}) o de registros ({DATA_RECORDS_FILE})."

        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)

        with open(DATA_RECORDS_FILE, encoding="utf-8") as f:
            records = json.load(f)

        item_index = state.get("item_index", {})

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            headers = {}

            # 1. Autenticación usando la ruta estándar de la API v2
            login_endpoints = ["/api/v2/users/auth", "/v2/auth/login", "/auth/login"]
            token = None

            for endpoint in login_endpoints:
                try:
                    auth_res = await client.post(
                        endpoint,
                        json={"username": JUB_USER, "password": JUB_PASS}
                    )
                    if auth_res.status_code in (200, 201):
                        data = auth_res.json()
                        token = data.get("access_token") or data.get("token") or data.get("accessToken")
                        if token:
                            headers["Authorization"] = f"Bearer {token}"
                        break
                except Exception:
                    continue

            if not token:
                print("Aviso: No se pudo autenticar vía token. Continuando sin cabecera Auth...")

            # 2. Verificar primero si ya existe un DataSource con ese nombre para evitar duplicados
            list_res = await client.get("/api/v2/datasources", headers=headers)
            source_id = None

            if list_res.status_code == 200:
                for ds in list_res.json():
                    if ds.get("name") == datasource_name:
                        source_id = ds.get("source_id") or ds.get("id")
                        break

            # Si no existe, lo creamos mediante POST
            if not source_id:
                datasource_payload = {
                    "name": datasource_name,
                    "description": datasource_description,
                    "format": format_type,
                    "connection_uri": connection_uri,
                }
                ds_res = await client.post("/api/v2/datasources", json=datasource_payload, headers=headers)
                if ds_res.status_code in (200, 201):
                    source_id = ds_res.json().get("source_id") or ds_res.json().get("id")
                else:
                    return f"Error crítico al registrar el DataSource ({ds_res.status_code}): {ds_res.text}"

            if not source_id:
                return "Error crítico: No se pudo obtener ni reutilizar un source_id válido."

            # 3. Ingestar Records mediante /api/v2/datasources/{source_id}/records
            for rec in records:
                rec["source_id"] = source_id
                # Mapear valores con el índice local guardado previamente
                rec["spatial_id"] = item_index.get(rec.get("spatial_id"), rec.get("spatial_id"))
                if "interest_ids" in rec:
                    rec["interest_ids"] = [
                        item_index.get(iid, iid) for iid in rec.get("interest_ids", [])
                    ]

            ingest_res = await client.post(f"/api/v2/datasources/{source_id}/records", json=records, headers=headers)
            if ingest_res.status_code not in (200, 201):
                return f"Error en la ingesta masiva de registros ({ingest_res.status_code}): {ingest_res.text}"

            # 4. Consultar Records de prueba mediante el DSL (`/api/v2/datasources/{source_id}/query`)
            query_payload = {
                "query": "jub.v1.VI(BENCENO)",
                "limit": 5,
                "skip": 0
            }
            query_res = await client.post(f"/api/v2/datasources/{source_id}/query", json=query_payload, headers=headers)
            
            query_results = []
            if query_res.status_code == 200:
                query_results = query_res.json()

            # 5. Actualizar archivo de estado local
            state["source_id"] = source_id
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)

            return json.dumps({
                "status": "success",
                "source_id": source_id,
                "records_ingested": len(records),
                "query_sample_results": query_results[:2] if isinstance(query_results, list) else query_results,
                "state_file": str(STATE_FILE.resolve())
            }, ensure_ascii=False)