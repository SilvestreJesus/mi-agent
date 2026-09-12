import csv
import io
import json
import os
import base64
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from fastmcp import FastMCP

from config import JUB_URL, JUB_USER, JUB_PASS, STATE_FILE

SOURCES_DIR = Path("sources")
IMAGES_DIR = Path("images")
DATA_DIR = Path("data")

OLLAMA_URL_INTERNO = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "llava")

_token: Optional[str] = None


async def get_auth_token() -> Optional[str]:
    """Obtiene o reutiliza el token Bearer usando el endpoint v2 oficial."""
    global _token
    if _token is None:
        async with httpx.AsyncClient(base_url=JUB_URL) as client:
            res = await client.post(
                "/api/v2/users/auth",
                json={"username": JUB_USER, "password": JUB_PASS}
            )
            if res.status_code in (200, 201):
                data = res.json()
                _token = data.get("access_token") or data.get("token") or data.get("accessToken")
    return _token


def resolve_existing_path(filename: str) -> Path:
    candidate = Path(filename)
    if candidate.exists():
        return candidate

    for folder in [SOURCES_DIR, Path("/app/sources"), IMAGES_DIR, Path("/app/images"), DATA_DIR, Path("/app")]:
        alt = folder / candidate.name
        if alt.exists():
            return alt

    return candidate


def _flatten_items(items: List[Dict[str, Any]]):
    for item in items:
        yield item
        if "children" in item and item["children"]:
            yield from _flatten_items(item["children"])


def find_column_by_candidates(headers: List[str], candidates: List[str]) -> Optional[str]:
    header_map = {h.lower().strip(): h for h in headers}
    for cand in candidates:
        if cand.lower().strip() in header_map:
            return header_map[cand.lower().strip()]
    return None


def normalize_str(val: Any) -> str:
    return str(val).strip() if val is not None else ""


def to_upper_snake(text: str) -> str:
    if not text: return "DESCONOCIDO"
    trans = str.maketrans("áéíóúÁÉÍÓÚñÑüÜ", "aeiouAEIOUnNuU")
    t = str(text).translate(trans)
    import re
    t = re.sub(r"([a-z])([A-Z])", r"\1_\2", t)
    return re.sub(r"[^A-Za-z0-9]+", "_", t).upper().strip("_")


def register(mcp: FastMCP):
    
    @mcp.tool(name="analizar_imagen_con_ia")
    async def analizar_imagen_con_ia(url_imagen: str, pregunta_o_instruccion: str) -> str:
        """Usa un modelo de Visión para analizar una imagen desde una URL."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as fetch_client:
                img_response = await fetch_client.get(url_imagen)
                img_response.raise_for_status()
                img_bytes = img_response.content

            img_base64 = base64.b64encode(img_bytes).decode('utf-8')
            payload = {
                "model": OLLAMA_VISION_MODEL,
                "prompt": pregunta_o_instruccion,
                "images": [img_base64],
                "stream": False
            }
            
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(f"{OLLAMA_URL_INTERNO}/api/generate", json=payload)
                response.raise_for_status()
                
            data = response.json()
            return json.dumps({
                "status": "success", "url": url_imagen, "analisis": data.get('response', 'Sin respuesta.')
            }, ensure_ascii=False, indent=2)
            
        except Exception as e:
            return json.dumps({"status": "error", "message": f"Error IA: {str(e)}"}, ensure_ascii=False)

    @mcp.tool(name="index_v2")
    async def index_v2(
        observatory_title: Optional[str] = None,
        observatory_description: Optional[str] = None,
        institution: Optional[str] = None,
        edition: Optional[str] = None,
        country: Optional[str] = None,
        csv_filename: Optional[str] = None,
        csv_content: Optional[str] = None,
        product_name_base: Optional[str] = None,    
        product_description_base: Optional[str] = None, 
        product_id_base: Optional[str] = None,        
        start_year: Optional[str] = None,
        end_year: Optional[str] = None,
        product_file_filename: Optional[str] = None,
        product_file_content: Optional[str] = None,
        datasource_name: Optional[str] = None,
        datasource_description: Optional[str] = None,
        source_id: Optional[str] = None,            
        image_url: Optional[str] = None,              
        user_id: str = "usr_system",
    ) -> str:
        """Herramienta v2 flexible: Permite crear Observatorios, Datasources o Indexación Integral según lo provisto."""
        
        steps_log: List[str] = []
        warnings: List[str] = []
        resumen: Dict[str, str] = {}

        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        path_csv = resolve_existing_path(csv_filename) if csv_filename else SOURCES_DIR / "datos.csv"
        if not path_csv.exists() and csv_content and csv_content.strip():
            with open(path_csv, "w", encoding="utf-8") as f:
                f.write(csv_content)
            steps_log.append(f"CSV guardado en: '{path_csv}'.")

        stem = path_csv.stem.replace("temp_", "")

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=120.0) as client:
            token = await get_auth_token()
            if not token:
                return json.dumps({"status": "error", "message": "Credenciales inválidas o error de autenticación con JUB API."})
            headers = {"Authorization": f"Bearer {token}"}

            observatory_id = None
            task_id = None

            # 1. CREAR OBSERVATORIO (Opcional si se proporcionan sus datos)
            if observatory_title and observatory_description:
                steps_log.append("[1/6] Creando Observatorio v2...")
                setup_payload = {
                    "title": observatory_title, 
                    "user_id": user_id, 
                    "description": observatory_description,
                    "metadata": {
                        "edition": edition or "2024", 
                        "country": country or "México", 
                        "institution": institution or "Institución General"
                    }
                }
                if image_url: setup_payload["image_url"] = image_url

                obs_res = await client.post("/api/v2/observatories/setup", json=setup_payload, headers=headers)
                if obs_res.status_code in (200, 201):
                    obs_data = obs_res.json()
                    observatory_id = obs_data.get("observatory_id")
                    task_id = obs_data.get("task_id")
                    resumen["observatorio"] = f"Creado (ID: {observatory_id})"
                else:
                    return json.dumps({"status": "error", "message": f"Fallo al crear observatorio ({obs_res.status_code})", "detalles": obs_res.text}, ensure_ascii=False)
            else:
                # Si no se pasó en esta llamada, intentamos leerlo del estado previo si existe
                if STATE_FILE.exists():
                    try:
                        with open(STATE_FILE, encoding="utf-8") as sf:
                            st_data = json.load(sf)
                            observatory_id = st_data.get("observatory_id")
                    except: pass

            item_index = {}
            # 2. CATÁLOGOS Y PRODUCTOS (Solo si hay observatorio y CSV)
            if observatory_id and path_csv.exists():
                steps_log.append("[2/6] Analizando CSV y Generando Catálogos Dinámicos...")
                with open(path_csv, encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    headers_csv = reader.fieldnames or []
                    rows = list(reader)

                col_mun = find_column_by_candidates(headers_csv, ["municipio", "nom_mun", "estado"])
                col_year = find_column_by_candidates(headers_csv, ["anio", "año", "year", "fecha"])
                col_valor = find_column_by_candidates(headers_csv, ["valor", "emisiones", "total", "cantidad"])

                spatial_seen, temporal_seen = set(), set()
                spatial_items, temporal_items = [], []

                for row in rows:
                    m_val = normalize_str(row.get(col_mun) if col_mun else (country or "General"))
                    s_code = to_upper_snake(m_val)
                    if s_code not in spatial_seen:
                        spatial_seen.add(s_code)
                        spatial_items.append({"name": m_val, "value": s_code, "code": len(spatial_seen), "value_type": "string"})

                    y_val = normalize_str(row.get(col_year) if col_year else (edition or "2024"))
                    import re
                    match = re.search(r"\b(19\d\d|20\d\d)\b", y_val)
                    y_clean = match.group(1) if match else (edition or "2024")
                    t_code = f"Y{y_clean}"
                    if t_code not in temporal_seen:
                        temporal_seen.add(t_code)
                        temporal_items.append({
                            "name": y_clean, "value": t_code, "code": int(y_clean) if y_clean.isdigit() else 2024,
                            "value_type": "datetime", "temporal_value": f"{y_clean}-01-01T00:00:00Z"
                        })

                catalogs_payload = {
                    "level": 0,
                    "catalogs": [
                        {"name": f"Spatial - {observatory_title or 'Catálogo'}", "value": "SPATIAL", "catalog_type": "spatial", "items": spatial_items},
                        {"name": f"Temporal - {observatory_title or 'Catálogo'}", "value": "TEMPORAL", "catalog_type": "temporal", "items": temporal_items}
                    ]
                }

                cat_res = await client.post(f"/api/v2/observatories/{observatory_id}/catalogs/bulk", json=catalogs_payload, headers=headers)
                catalog_ids = cat_res.json().get("catalog_ids", []) if cat_res.status_code in (200, 201) else []
                
                for cid in catalog_ids:
                    c_data = await client.get(f"/api/v2/catalogs/{cid}", headers=headers)
                    if c_data.status_code == 200:
                        for item in _flatten_items(c_data.json().get("items", [])):
                            if item.get("value") and (item.get("catalog_item_id") or item.get("id")):
                                item_index[item.get("value")] = item.get("catalog_item_id") or item.get("id")

                # 3. PRODUCTOS (Si se especificaron bases de productos o años)
                if product_name_base and start_year and end_year:
                    steps_log.append("[3/6] Registrando productos anuales...")
                    try:
                        s_year, e_year = int(start_year), int(end_year)
                    except:
                        s_year, e_year = 2024, 2024
                    
                    products_list = [{"name": f"Dataset {product_name_base} {s_year}-{e_year}", "description": product_description_base or "", "catalog_item_ids": []}]
                    if product_id_base:
                        products_list[0]["product_id"] = f"{product_id_base}-dataset"

                    for year in range(s_year, e_year + 1):
                        cat_year_id = item_index.get(f"Y{year}")
                        y_prod = {
                            "name": f"{product_name_base} — {year}", "description": f"{product_description_base or ''} - {year}",
                            "catalog_item_ids": [cat_year_id] if cat_year_id else []
                        }
                        if product_id_base:
                            y_prod["product_id"] = f"{product_id_base}-{year}"
                        products_list.append(y_prod)
                    
                    prod_res = await client.post(f"/api/v2/observatories/{observatory_id}/products/bulk", json={"products": products_list}, headers=headers)
                    created_products = prod_res.json().get("products", []) if prod_res.status_code in (200, 201) else []
                    resumen["productos"] = f"{len(created_products) if created_products else len(products_list)} creados"

                    # 4. SUBIDA DE ARCHIVO AL PRODUCTO PRINCIPAL
                    if created_products and (product_file_filename or product_file_content):
                        target_product_id = created_products[0].get("product_id") or created_products[0].get("id")
                        file_bytes = product_file_content.encode('utf-8') if product_file_content else None
                        file_name = product_file_filename or "recurso.png"

                        if not file_bytes and product_file_filename:
                            res_path = resolve_existing_path(product_file_filename)
                            if res_path.exists():
                                with open(res_path, "rb") as f_bin: file_bytes = f_bin.read()
                                file_name = res_path.name

                        if file_bytes:
                            upload_res = await client.post(
                                f"/api/v2/products/{target_product_id}/upload",
                                data={"user_id": user_id}, files={"file": (file_name, file_bytes)}, headers=headers,
                            )
                            if upload_res.status_code in (200, 201, 202): resumen["archivos_recursos"] = f"Archivo '{file_name}' subido exitosamente"
                            else: warnings.append(f"Error al subir recurso: {upload_res.text[:100]}")

            # 5. CREACIÓN DE DATASOURCE Y REGISTROS EN LOTES (CHUNKS)
            created_source_id = source_id or f"src_{stem}"
            if datasource_name and path_csv.exists():
                steps_log.append("[5/6] Creando DataSource y procesando registros en lotes...")
                ds_payload = {"name": datasource_name, "description": datasource_description or "", "format": "csv"}
                if source_id: ds_payload["source_id"] = source_id

                ds_res = await client.post("/api/v2/datasources", json=ds_payload, headers=headers)
                if ds_res.status_code in (200, 201):
                    created_source_id = ds_res.json().get("source_id", created_source_id)
                resumen["datasource"] = f"ID: {created_source_id}"

                with open(path_csv, encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    headers_csv = reader.fieldnames or []
                    col_mun = find_column_by_candidates(headers_csv, ["municipio", "nom_mun", "estado"])
                    col_year = find_column_by_candidates(headers_csv, ["anio", "año", "year", "fecha"])
                    col_valor = find_column_by_candidates(headers_csv, ["valor", "emisiones", "total", "cantidad"])
                    rows = list(reader)

                records_list = []
                for idx, row in enumerate(rows):
                    m_val = to_upper_snake(normalize_str(row.get(col_mun) if col_mun else (country or "General")))
                    y_val = normalize_str(row.get(col_year) if col_year else (edition or "2024"))
                    try: v_parsed = float(row.get(col_valor) if col_valor else 0)
                    except: v_parsed = 0.0

                    records_list.append({
                        "record_id": f"rec_{idx+1}",
                        "spatial_id": item_index.get(m_val, m_val),
                        "temporal_id": f"{y_val}-01-01T00:00:00Z",
                        "interest_ids": [],
                        "numerical_interest_ids": {"VALOR": v_parsed},
                        "raw_payload": row,
                    })

                # Enviar en bloques de 500 registros para evitar timeouts con CSVs grandes (ej. 3,000+ filas)
                CHUNK_SIZE = 500
                success_chunks = 0
                for i in range(0, len(records_list), CHUNK_SIZE):
                    chunk = records_list[i:i + CHUNK_SIZE]
                    rec_res = await client.post(f"/api/v2/datasources/{created_source_id}/records", json=chunk, headers=headers)
                    if rec_res.status_code in (200, 201):
                        success_chunks += 1
                    else:
                        warnings.append(f"Error en lote {i//CHUNK_SIZE + 1}: {rec_res.text[:100]}")
                
                resumen["registros"] = f"{len(records_list)} registros subidos ({success_chunks} lotes)"

            # 6. FINALIZAR TAREA DE SETUP
            if task_id:
                steps_log.append("[6/6] Finalizando tarea de setup...")
                await client.post(f"/api/v2/tasks/{task_id}/complete", json={"success": True}, headers=headers)

            state_data = {}
            if STATE_FILE.exists():
                try:
                    with open(STATE_FILE, encoding="utf-8") as sf: state_data = json.load(sf)
                except: pass
            
            if observatory_id: state_data["observatory_id"] = observatory_id
            if created_source_id: state_data["source_id"] = created_source_id
            if item_index: state_data["item_index"] = item_index
            
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2, ensure_ascii=False)

            return json.dumps({
                "status": "success", 
                "observatory_id": observatory_id, 
                "source_id": created_source_id,
                "steps_completed": steps_log,
                "resumen": resumen, 
                "advertencias": warnings, 
                "mensaje": "Proceso ejecutado con éxito de forma independiente."
            }, ensure_ascii=False, indent=2)

    # ---------------------------------------------------------
    # HERRAMIENTA 3: CONSULTA DE SERVICIOS
    # ---------------------------------------------------------
    @mcp.tool(name="consultar_servicios_svc")
    async def consultar_servicios_svc(
        query: str, 
        limit: int = 10,
        skip: int = 0
    ) -> str:
        """Consulta los servicios vinculados al Observatorio activo o ejecuta una consulta DSL."""
        
        if not query:
            return "Error: Debes proporcionar una cadena de consulta DSL válida en el parámetro 'query'."

        if not STATE_FILE.exists():
            return f"Error: No se encontró el archivo de estado ({STATE_FILE}). Ejecuta los pasos previos."

        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)

        observatory_id = state.get("observatory_id")
        if not observatory_id:
            return "Error: No se encontró 'observatory_id' en .state.json."

        token = await get_auth_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        async with httpx.AsyncClient(base_url=JUB_URL, headers=headers, timeout=60.0) as client:
            try:
                search_payload = {
                    "query": query,
                    "observatory_id": observatory_id,
                    "limit": limit,
                    "skip": skip
                }
                search_res = await client.post("/api/v2/search", json=search_payload)

                query_results = []
                if search_res.status_code == 200:
                    query_results = search_res.json()
                else:
                    svc_res = await client.get(f"/api/v2/observatories/{observatory_id}/services")
                    if svc_res.status_code == 200:
                        query_results = svc_res.json()
                    else:
                        return f"Error al consultar servicios ({svc_res.status_code}): {svc_res.text}"

                return json.dumps({
                    "status": "success",
                    "query_executed": query,
                    "observatory_id": observatory_id,
                    "total_results": len(query_results) if isinstance(query_results, list) else 1,
                    "results": query_results
                }, ensure_ascii=False, indent=2)

            except Exception as e:
                return f"Error inesperado al consultar los servicios vía HTTP: {str(e)}"