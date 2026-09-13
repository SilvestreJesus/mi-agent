import csv
import io
import json
import os
import base64
import re
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
            try:
                res = await client.post(
                    "/api/v2/users/auth",
                    json={"username": JUB_USER, "password": JUB_PASS}
                )
                if res.status_code in (200, 201):
                    data = res.json()
                    _token = data.get("access_token") or data.get("token") or data.get("accessToken")
            except Exception:
                pass
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


def normalize_str(val: Any) -> str:
    return str(val).strip() if val is not None else ""


def to_upper_snake(text: str) -> str:
    if not text: return "DESCONOCIDO"
    trans = str.maketrans("áéíóúÁÉÍÓÚñÑüÜ", "aeiouAEIOUnNuU")
    t = str(text).translate(trans)
    t = re.sub(r"([a-z])([A-Z])", r"\1_\2", t)
    return re.sub(r"[^A-Za-z0-9]+", "_", t).upper().strip("_")


def extraer_anios_csv(path_csv: Path) -> tuple[int, int]:
    """Analiza dinámicamente el CSV para encontrar el año mínimo y máximo."""
    anios = []
    if path_csv.exists():
        try:
            with open(path_csv, encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                headers = [h.lower().strip() for h in (reader.fieldnames or [])]
                col_year = next((h for h in headers if h in ["anio", "año", "year", "anios_reporte", "yfd"]), None)
                
                for row in reader:
                    if col_year:
                        val = normalize_str(row.get(col_year))
                        match = re.search(r"\b(19\d\d|20\d\d)\b", val)
                        if match:
                            anios.append(int(match.group(1)))
                    else:
                        for v in row.values():
                            match = re.search(r"\b(19\d\d|20\d\d)\b", str(v))
                            if match:
                                anios.append(int(match.group(1)))
        except Exception:
            pass

    if anios:
        return min(anios), max(anios)
    
    import datetime
    current_y = datetime.datetime.now().year
    return current_y, current_y


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

    # ---------------------------------------------------------
    # 1. HERRAMIENTA INDEPENDIENTE: CREAR OBSERVATORIO Y PRODUCTOS
    # ---------------------------------------------------------
    @mcp.tool(name="crear_observatorio_v2")
    async def crear_observatorio_v2(
        observatory_title: str,
        observatory_description: str,
        institution: str,
        edition: str,
        country: str,
        product_name_base: str,
        product_description_base: str,
        product_id_base: Optional[str] = None,
        start_year: Optional[str] = None,
        end_year: Optional[str] = None,
        csv_filename: Optional[str] = None,
        image_url: Optional[str] = None,
        user_id: str = "usr_system",
    ) -> str:
        """Crea exclusivamente el Observatorio, sus catálogos y productos anuales dinámicos en JUB."""
        token = await get_auth_token()
        if not token:
            return json.dumps({"status": "error", "message": "Error de autenticación con JUB API."}, ensure_ascii=False)

        headers = {"Authorization": f"Bearer {token}"}
        resumen = {}

        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)

        s_y, e_y = None, None
        if csv_filename:
            path_csv = resolve_existing_path(csv_filename)
            if path_csv.exists():
                s_y, e_y = extraer_anios_csv(path_csv)

        try:
            s_year_final = int(start_year) if start_year else (s_y if s_y else 2024)
            end_year_final = int(end_year) if end_year else (e_y if e_y else 2024)
        except ValueError:
            s_year_final, end_year_final = 2024, 2024

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=90.0) as client:
            setup_payload = {
                "title": observatory_title,
                "user_id": user_id,
                "description": observatory_description,
                "metadata": {"edition": edition, "country": country, "institution": institution}
            }
            if image_url: setup_payload["image_url"] = image_url

            obs_res = await client.post("/api/v2/observatories/setup", json=setup_payload, headers=headers)
            if obs_res.status_code not in (200, 201):
                return json.dumps({"status": "error", "message": f"Fallo al crear observatorio ({obs_res.status_code})", "detalles": obs_res.text}, ensure_ascii=False)

            obs_data = obs_res.json()
            observatory_id = obs_data.get("observatory_id")
            task_id = obs_data.get("task_id")
            resumen["observatorio"] = f"Creado (ID: {observatory_id})"

            # Catálogos básicos
            catalogs_payload = {
                "level": 0,
                "catalogs": [
                    {"name": f"Spatial - {observatory_title}", "value": "SPATIAL", "catalog_type": "spatial", "items": [{"name": country, "value": country, "code": 1, "value_type": "string"}]},
                    {"name": f"Temporal - {observatory_title}", "value": "TEMPORAL", "catalog_type": "temporal", "items": [{"name": edition, "value": f"Y{edition}", "code": 1, "value_type": "datetime", "temporal_value": f"{edition}-01-01T00:00:00Z"}]}
                ]
            }
            await client.post(f"/api/v2/observatories/{observatory_id}/catalogs/bulk", json=catalogs_payload, headers=headers)

            # Productos con rango dinámico
            products_list = [{
                "name": f"Dataset {product_name_base} {s_year_final}-{end_year_final}",
                "description": f"Dataset completo de {product_description_base}",
                "catalog_item_ids": []
            }]
            if product_id_base: products_list[0]["product_id"] = f"{product_id_base}-dataset"

            for year in range(s_year_final, end_year_final + 1):
                y_prod = {"name": f"{product_name_base} — {year}", "description": f"{product_description_base} - Periodo {year}", "catalog_item_ids": []}
                if product_id_base: y_prod["product_id"] = f"{product_id_base}-{year}"
                products_list.append(y_prod)

            prod_res = await client.post(f"/api/v2/observatories/{observatory_id}/products/bulk", json={"products": products_list}, headers=headers)
            created_products = prod_res.json().get("products", []) if prod_res.status_code in (200, 201) else []
            resumen["productos"] = f"{len(created_products)} productos creados (Rango: {s_year_final}-{end_year_final})"

            if task_id:
                await client.post(f"/api/v2/tasks/{task_id}/complete", json={"success": True, "message": f"Aprovisionamiento completado para {observatory_title}"}, headers=headers)

            state_data = {}
            if STATE_FILE.exists():
                try:
                    with open(STATE_FILE, encoding="utf-8") as sf: state_data = json.load(sf)
                except: pass
            state_data["observatory_id"] = observatory_id
            state_data["task_id"] = task_id
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2, ensure_ascii=False)

            return json.dumps({
                "status": "success", "observatory_id": observatory_id, "resumen": resumen
            }, ensure_ascii=False, indent=2)

    # ---------------------------------------------------------
    # 2. HERRAMIENTA INDEPENDIENTE: CREAR DATASOURCE Y REGISTROS
    # ---------------------------------------------------------
    @mcp.tool(name="crear_datasource_v2")
    async def crear_datasource_v2(
        datasource_name: str,
        datasource_description: str,
        csv_filename: Optional[str] = None,
        csv_content: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> str:
        """Crea exclusivamente un DataSource independiente e ingesta los registros del CSV."""
        token = await get_auth_token()
        if not token:
            return json.dumps({"status": "error", "message": "Error de autenticación con JUB API."}, ensure_ascii=False)

        headers = {"Authorization": f"Bearer {token}"}
        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        path_csv = resolve_existing_path(csv_filename) if csv_filename else SOURCES_DIR / "datos.csv"
        if not path_csv.exists() and csv_content and csv_content.strip():
            with open(path_csv, "w", encoding="utf-8") as f:
                f.write(csv_content)

        stem = path_csv.stem.replace("temp_", "") if path_csv.exists() else "datos"

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=120.0) as client:
            ds_payload = {"name": datasource_name, "description": datasource_description, "format": "csv"}
            if source_id: ds_payload["source_id"] = source_id

            ds_res = await client.post("/api/v2/datasources", json=ds_payload, headers=headers)
            created_source_id = ds_res.json().get("source_id", source_id or f"src_{stem}") if ds_res.status_code in (200, 201) else (source_id or f"src_{stem}")

            records_list = []
            if path_csv.exists():
                with open(path_csv, encoding="utf-8-sig") as f:
                    r_csv = csv.DictReader(f)
                    for idx, row in enumerate(r_csv):
                        val_raw = row.get("valor") or row.get("emisiones") or row.get("cantidad_kg") or 0
                        try: val_parsed = float(val_raw)
                        except: val_parsed = 0.0

                        anio_raw = str(row.get("anio") or row.get("año") or "2024")
                        muni_raw = str(row.get("municipio") or row.get("Municipio") or "General")

                        records_list.append({
                            "record_id": f"rec_{idx+1}",
                            "spatial_id": muni_raw.upper().replace(" ", "_"),
                            "temporal_id": f"{anio_raw}-01-01T00:00:00Z",
                            "interest_ids": [],
                            "numerical_interest_ids": {"VALOR": val_parsed},
                            "raw_payload": row,
                        })

            success_chunks = 0
            if records_list:
                CHUNK_SIZE = 500
                for i in range(0, len(records_list), CHUNK_SIZE):
                    chunk = records_list[i:i + CHUNK_SIZE]
                    rec_res = await client.post(f"/api/v2/datasources/{created_source_id}/records", json=chunk, headers=headers)
                    if rec_res.status_code in (200, 201): success_chunks += 1

            state_data = {}
            if STATE_FILE.exists():
                try:
                    with open(STATE_FILE, encoding="utf-8") as sf: state_data = json.load(sf)
                except: pass
            state_data["source_id"] = created_source_id
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2, ensure_ascii=False)

            return json.dumps({
                "status": "success",
                "source_id": created_source_id,
                "registros_subidos": len(records_list),
                "lotes_exitosos": success_chunks,
                "mensaje": "DataSource y registros procesados correctamente de forma independiente."
            }, ensure_ascii=False, indent=2)

    # ---------------------------------------------------------
    # 3. HERRAMIENTA INTEGRAL: INDEXACIÓN COMPLETA
    # ---------------------------------------------------------
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
        """Herramienta integral v2: Permite indexar Observatorio y Datasource de forma conjunta."""
        steps_log = []
        resumen = {}

        obs_id = None
        if observatory_title and observatory_description:
            steps_log.append("Ejecutando creación de observatorio...")
            obs_res_str = await crear_observatorio_v2(
                observatory_title=observatory_title,
                observatory_description=observatory_description,
                institution=institution or "Institución General",
                edition=edition or "2024",
                country=country or "México",
                product_name_base=product_name_base or observatory_title,
                product_description_base=product_description_base or observatory_description,
                product_id_base=product_id_base,
                start_year=start_year,
                end_year=end_year,
                csv_filename=csv_filename,
                image_url=image_url,
                user_id=user_id
            )
            obs_res_json = json.loads(obs_res_str)
            obs_id = obs_res_json.get("observatory_id")
            resumen["observatorio"] = obs_res_json.get("resumen", {}).get("observatorio")

        ds_id = None
        if datasource_name and (csv_filename or csv_content):
            steps_log.append("Ejecutando creación de datasource e ingesta de CSV...")
            ds_res_str = await crear_datasource_v2(
                datasource_name=datasource_name,
                datasource_description=datasource_description or "Datos procesados",
                csv_filename=csv_filename,
                csv_content=csv_content,
                source_id=source_id
            )
            ds_res_json = json.loads(ds_res_str)
            ds_id = ds_res_json.get("source_id")
            resumen["datasource"] = f"ID: {ds_id} ({ds_res_json.get('registros_subidos', 0)} registros)"

        return json.dumps({
            "status": "success",
            "observatory_id": obs_id,
            "source_id": ds_id,
            "steps_completed": steps_log,
            "resumen": resumen,
            "mensaje": "Indexación integral ejecutada con éxito."
        }, ensure_ascii=False, indent=2)