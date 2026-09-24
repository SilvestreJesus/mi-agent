import csv
import json
from pathlib import Path
from typing import Optional

import httpx
from fastmcp import FastMCP

from config import JUB_PASS, JUB_URL, JUB_USER, STATE_FILE

SOURCES_DIR = Path("sources")
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
        csv_filename: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> str:
        """Crea el DataSource, lo enlaza al Observatorio, mapea registros con el índice e ingesta masivamente por lotes."""
        
        # 1. Determinar y resolver la ruta del archivo CSV o registros
        target_file = csv_filename or records_filename
        path_csv = (
            resolve_existing_path(target_file)
            if target_file
            else SOURCES_DIR / "datos.csv"
        )

        if not STATE_FILE.exists():
            return f"Error: No se encontró el archivo de estado {STATE_FILE}."

        if not path_csv.exists():
            return f"Error: No se encontró el archivo de datos en {path_csv}."

        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)

        observatory_id = state.get("observatory_id")
        item_index = state.get("item_index", {})

        # Si no se provee connection_uri, lo inferimos de forma dinámica
        if not connection_uri:
            connection_uri = f"file://sources/{path_csv.name}"

        async with httpx.AsyncClient(
            base_url=JUB_URL, timeout=300.0
        ) as client:
            headers = {}

            # Auth
            try:
                auth_res = await client.post(
                    "/api/v2/users/auth",
                    json={"username": JUB_USER, "password": JUB_PASS},
                )
                if auth_res.status_code in (200, 201):
                    data = auth_res.json()
                    token = data.get("access_token") or data.get("token")
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
            except Exception as e:
                print(f"Auth error: {e}")

            # 1. Registrar DataSource o recuperarlo si ya existe (Evita error 409)
            created_source_id = None
            ds_payload = {
                "name": datasource_name,
                "description": datasource_description,
                "format": format_type,
                "connection_uri": connection_uri,
            }
            if source_id:
                ds_payload["source_id"] = source_id

            ds_res = await client.post("/api/v2/datasources", json=ds_payload, headers=headers)
            
            # Si se creó con éxito
            if ds_res.status_code in (200, 201):
                created_source_id = ds_res.json().get("source_id") or ds_res.json().get("id")
            
            # Si la API responde con conflicto (409) o el mensaje indica duplicidad, lo buscamos en el listado
            elif ds_res.status_code == 409 or "already" in ds_res.text.lower() or "exists" in ds_res.text.lower():
                list_res = await client.get("/api/v2/datasources", headers=headers)
                if list_res.status_code == 200:
                    for ds in list_res.json():
                        if ds.get("name") == datasource_name:
                            created_source_id = ds.get("source_id") or ds.get("id")
                            break

            if not created_source_id:
                return f"Error: No se pudo crear ni recuperar el DataSource. Respuesta del servidor: {ds_res.text}"

            # 2. Enlazar al Observatorio (si no está vinculado previamente)
            if observatory_id:
                await client.post(
                    f"/api/v2/observatories/{observatory_id}/datasources",
                    json={"source_id": created_source_id},
                    headers=headers,
                )

            # 3. Leer CSV, mapear registros y preparar la lista
            records_list = []
            with open(path_csv, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader):
                    val_raw = row.get("valor") or row.get("emisiones") or 0
                    try:
                        val_parsed = float(val_raw)
                    except (ValueError, TypeError):
                        val_parsed = 0.0

                    anio_raw = str(row.get("anio") or row.get("año") or "2024")
                    muni_raw = (row.get("municipio") or row.get("Municipio") or "General")
                    
                    s_id_raw = muni_raw.upper().replace(" ", "_")
                    spatial_id = item_index.get(s_id_raw, s_id_raw)

                    records_list.append({
                        "record_id": f"rec_{idx+1}",
                        "source_id": created_source_id,
                        "spatial_id": spatial_id,
                        "temporal_id": f"{anio_raw}-01-01T00:00:00Z",
                        "interest_ids": [],
                        "numerical_interest_ids": {"VALOR": val_parsed},
                        "raw_payload": row,
                    })

            # 4. Ingesta masiva dividida en lotes (Batches de 1000)
            batch_size = 1000
            total_uploaded = 0
            for i in range(0, len(records_list), batch_size):
                batch = records_list[i:i + batch_size]
                ingest_res = await client.post(
                    f"/api/v2/datasources/{created_source_id}/records",
                    json=batch,
                    headers=headers,
                )
                if ingest_res.status_code not in (200, 201):
                    return f"Error en ingesta de lote ({ingest_res.status_code}): {ingest_res.text}"
                total_uploaded += len(batch)

            # 5. Consulta DSL de prueba
            query_res = await client.post(
                f"/api/v2/datasources/{created_source_id}/query",
                json={"query": test_dsl_query, "limit": 5, "skip": 0},
                headers=headers,
            )
            query_results = (
                query_res.json() if query_res.status_code == 200 else []
            )

            # 6. Actualizar estado (.state.json)
            state["source_id"] = created_source_id
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)

            return json.dumps(
                {
                    "status": "success",
                    "source_id": created_source_id,
                    "records_ingested": total_uploaded,
                    "connection_uri_used": connection_uri,
                    "query_sample_results": (
                        query_results[:2]
                        if isinstance(query_results, list)
                        else query_results
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )