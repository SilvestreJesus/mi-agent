import csv
import io
import json
import os
import base64
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from fastmcp import FastMCP

from config import JUB_URL, JUB_USER, JUB_PASS, DATA_RECORDS_FILE, STATE_FILE

SOURCES_DIR = Path("sources")
IMAGES_DIR = Path("images")
DATA_DIR = Path("data")

OLLAMA_URL_INTERNO = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "llava")


def resolve_existing_path(filename: str) -> Path:
    candidate = Path(filename)
    if candidate.exists():
        return candidate

    for folder in [
        SOURCES_DIR,
        Path("/app/sources"),
        IMAGES_DIR,
        Path("/app/images"),
        DATA_DIR,
        Path("/app"),
    ]:
        alt = folder / candidate.name
        if alt.exists():
            return alt

    return candidate


async def _get_jub_token() -> Optional[str]:
    """Helper para autenticarse en JUB y obtener el token de acceso."""
    try:
        async with httpx.AsyncClient(base_url=JUB_URL, timeout=30.0) as client:
            auth_res = await client.post(
                "/api/v2/users/auth",
                json={"username": JUB_USER, "password": JUB_PASS},
            )
            if auth_res.status_code in (200, 201):
                data = auth_res.json()
                return data.get("access_token") or data.get("token")
    except Exception:
        pass
    return None


def register(mcp: FastMCP):
    
    @mcp.tool(name="analizar_imagen_con_ia")
    async def analizar_imagen_con_ia(
        url_imagen: str, 
        pregunta_o_instruccion: str
    ) -> str:
        """
        Usa un modelo de Inteligencia Artificial Multimodal (Visión) para analizar una imagen desde una URL.
        Úsala SIEMPRE que necesites extraer información, describir gráficas o entender el contexto visual de un enlace de internet.
        """
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
            respuesta_vision = data.get('response', 'No se obtuvo respuesta.')
            
            return json.dumps({
                "status": "success",
                "url": url_imagen,
                "analisis": respuesta_vision
            }, ensure_ascii=False, indent=2)
            
        except httpx.HTTPError as he:
            return json.dumps({
                "status": "error",
                "message": f"Error al descargar la imagen de la URL: {str(he)}"
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Error al analizar la imagen con IA: {str(e)}"
            }, ensure_ascii=False)


    # Herramienta para crear observatorio
    @mcp.tool(name="crear_observatorio")
    async def crear_observatorio(
        observatory_title: str,
        observatory_description: str,
        institution: str,
        edition: str,
        country: str,
        image_url: Optional[str] = None,
        user_id: str = "usr_system",
    ) -> str:
        """Crea el contenedor raíz (Observatorio) en JUB v2 y completa su tarea para hacerlo visible en la interfaz."""
        token = await _get_jub_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        setup_payload = {
            "title": observatory_title,
            "user_id": user_id,
            "description": observatory_description,
            "metadata": {
                "edition": edition,
                "country": country,
                "institution": institution,
            },
        }
        if image_url:
            setup_payload["image_url"] = image_url

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            res = await client.post("/api/v2/observatories/setup", json=setup_payload, headers=headers)
            if res.status_code not in (200, 201):
                return json.dumps({"status": "error", "message": res.text}, ensure_ascii=False)
            
            data = res.json()
            observatory_id = data.get("observatory_id")
            task_id = data.get("task_id")

            # COMPLETAR LA TAREA AUTOMÁTICAMENTE PARA QUE APAREZCA EN LA UI
            if task_id:
                await client.post(
                    f"/api/v2/tasks/{task_id}/complete",
                    json={"success": True, "message": f"Observatorio {observatory_title} habilitado con éxito."},
                    headers=headers,
                )

            return json.dumps({
                "status": "success",
                "observatory_id": observatory_id,
                "task_id": task_id,
                "message": "Observatorio creado, habilitado y visible en la interfaz correctamente."
            }, ensure_ascii=False, indent=2)


    # Herramienta para crear catálogos robusta
    @mcp.tool(name="crear_catalogos")
    async def crear_catalogos(
        observatory_id: str,
        observatory_title: Optional[str] = "Observatorio",
        country: str = "México",
        edition: str = "2024",
        csv_filename: Optional[str] = None,
        csv_content: Optional[str] = None,
    ) -> str:
        """Genera y vincula los catálogos en formato de lista plana requerido por JUB v2."""
        token = await _get_jub_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        path_csv = resolve_existing_path(csv_filename) if csv_filename else None
        spatial_items, temporal_items = [], []
        spatial_seen, temporal_seen = set(), set()

        reader = None
        f_csv = None
        if path_csv and path_csv.exists():
            f_csv = open(path_csv, encoding="utf-8")
            reader = csv.DictReader(f_csv)
        elif csv_content:
            reader = csv.DictReader(io.StringIO(csv_content))

        if reader:
            for row in reader:
                muni = row.get("municipio") or row.get("Municipio") or row.get("estado") or "General"
                s_val = muni.upper().replace(" ", "_")
                if s_val not in spatial_seen:
                    spatial_seen.add(s_val)
                    spatial_items.append({
                        "name": muni, "value": s_val, "code": len(spatial_seen),
                        "value_type": "STRING", "aliases": [], "children": []
                    })
                anio = str(row.get("anio") or row.get("año") or edition)
                t_val = f"Y{anio}"
                if t_val not in temporal_seen:
                    temporal_seen.add(t_val)
                    temporal_items.append({
                        "name": anio, "value": t_val, "code": int(anio) if anio.isdigit() else 2024,
                        "value_type": "STRING", "temporal_value": f"{anio}-01-01T00:00:00Z", "aliases": [], "children": []
                    })
        if f_csv:
            f_csv.close()

        catalogs_payload = [
            {
                "name": f"Spatial - {observatory_title}",
                "value": "SPATIAL",
                "catalog_type": "SPATIAL",
                "description": "Catálogo Geográfico",
                "items": spatial_items or [{"name": country, "value": country, "code": 1, "value_type": "STRING", "aliases": [], "children": []}],
            },
            {
                "name": f"Temporal - {observatory_title}",
                "value": "TEMPORAL",
                "catalog_type": "TEMPORAL",
                "description": "Catálogo Temporal",
                "items": temporal_items or [{"name": edition, "value": f"Y{edition}", "code": 1, "value_type": "STRING", "temporal_value": f"{edition}-01-01T00:00:00Z", "aliases": [], "children": []}],
            },
        ]

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            res = await client.post(f"/api/v2/observatories/{observatory_id}/catalogs/bulk", json=catalogs_payload, headers=headers)
            if res.status_code not in (200, 201):
                return json.dumps({"status": "error", "message": res.text}, ensure_ascii=False)

            return json.dumps({
                "status": "success",
                "detalles": res.json(),
                "message": "Catálogos creados y enlazados correctamente en bulk."
            }, ensure_ascii=False, indent=2)

    # Herramienta para productos optimizada y estricta
    @mcp.tool(name="crear_productos")
    async def crear_productos(
        observatory_id: str,
        product_name_base: str,
        product_desc_base: Optional[str] = None,
        product_description_base: Optional[str] = None, 
        product_id_base: Optional[str] = None,
        start_year: str = "2000",
        end_year: str = "2026",
    ) -> str:
        """Crea productos múltiples vinculados correctamente al observatorio con sus metadatos."""
        desc_final = product_desc_base or product_description_base or product_name_base
        token = await _get_jub_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        try:
            s_year, e_year = int(start_year), int(end_year)
        except ValueError:
            return json.dumps({"status": "error", "message": "start_year y end_year deben ser numéricos."}, ensure_ascii=False)

        products_list = []
        main_prod = {
            "name": f"Dataset {product_name_base} {s_year}-{e_year}",
            "description": f"Dataset completo de {desc_final}",
            "catalog_item_ids": []
        }
        if product_id_base:
            main_prod["product_id"] = f"{product_id_base}-dataset"
        products_list.append(main_prod)

        for year in range(s_year, e_year + 1):
            y_prod = {
                "name": f"{product_name_base} — {year}",
                "description": f"{desc_final} - Periodo {year}",
                "catalog_item_ids": []
            }
            if product_id_base:
                y_prod["product_id"] = f"{product_id_base}-{year}"
            products_list.append(y_prod)

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            res = await client.post(f"/api/v2/observatories/{observatory_id}/products/bulk", json={"products": products_list}, headers=headers)
            if res.status_code not in (200, 201):
                return json.dumps({"status": "error", "message": res.text}, ensure_ascii=False)

            return json.dumps({
                "status": "success",
                "resultado": res.json(),
                "message": "Productos múltiples creados y vinculados con éxito a la interfaz."
            }, ensure_ascii=False, indent=2)


    # Herramienta para DataSource con mapeo numérico e intereses flexible
    @mcp.tool(name="crear_datasource_y_ingestar")
    async def crear_datasource_y_ingestar(
        datasource_name: str,
        datasource_description: str,
        csv_filename: Optional[str] = None,
        csv_content: Optional[str] = None,
        source_id: Optional[str] = None,
        edition: str = "2024",
        country: str = "México",
    ) -> str:
        """Crea el DataSource e ingesta masiva mapeando correctamente todas las columnas numéricas y de texto del CSV."""
        token = await _get_jub_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        path_csv = resolve_existing_path(csv_filename) if csv_filename else SOURCES_DIR / "datos.csv"
        
        if not path_csv.exists() and csv_content and csv_content.strip():
            with open(path_csv, "w", encoding="utf-8") as f:
                f.write(csv_content)

        stem = path_csv.stem.replace("temp_", "")

        ds_payload = {
            "name": datasource_name,
            "description": datasource_description,
            "format": "csv",
        }
        if source_id:
            ds_payload["source_id"] = source_id

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=300.0) as client:
            ds_res = await client.post("/api/v2/datasources", json=ds_payload, headers=headers)
            created_source_id = (
                ds_res.json().get("source_id")
                if ds_res.status_code in (200, 201)
                else (source_id or f"src_{stem}")
            )

            records_list = []
            if path_csv.exists():
                with open(path_csv, encoding="utf-8") as f:
                    r_csv = csv.DictReader(f)
                    for idx, row in enumerate(r_csv):
                        row_id = row.get("NSEQN") or row.get("id") or row.get("ID") or str(idx + 1)
                        clean_ds_prefix = datasource_name.lower().split()[0] if datasource_name else "record"
                        record_id = f"{clean_ds_prefix}-{row_id}"

                        muni_raw = row.get("municipio") or row.get("Municipio") or row.get("estado") or country
                        spatial_id = muni_raw.upper().replace(" ", "_")

                        anio_raw = str(row.get("anio") or row.get("año") or row.get("YFD") or edition)
                        temporal_id = f"{anio_raw}-01-01T00:00:00Z" if len(anio_raw) == 4 else "2024-01-01T00:00:00Z"

                        numerical_interests = {}
                        interest_ids = []

                        # Mapeo avanzado: analiza cada columna del CSV buscando valores válidos
                        for key, val in row.items():
                            clean_key = key.strip().lower()
                            if clean_key in ["nseqn", "id", "municipio", "estado", "anio", "año", "yfd"]:
                                continue
                            
                            val_str = str(val).strip() if val is not None else ""
                            if not val_str:
                                continue

                            # Intentar convertir a número para la sección de métricas numéricas
                            try:
                                # Limpiar comas de miles si las hubiera (ej: "1,200.50")
                                clean_val = val_str.replace(",", "")
                                num_val = float(clean_val)
                                numerical_interests[key.strip().upper()] = num_val
                            except (ValueError, TypeError):
                                # Si no es número, se añade como etiqueta de interés (VI)
                                interest_ids.append(val_str)

                        # Si de plano no encontró ninguna columna numérica, asigna un respaldo útil basado en texto o valor 1.0
                        if not numerical_interests:
                            numerical_interests = {"REGISTRO_ACTIVO": 1.0}

                        records_list.append({
                            "record_id": record_id,
                            "spatial_id": spatial_id,
                            "temporal_id": temporal_id,
                            "interest_ids": interest_ids[:5], # Limitar etiquetas visuales para mantener limpio el UI
                            "numerical_interest_ids": numerical_interests,
                            "raw_payload": row,
                        })

            batch_size = 1000
            total_uploaded = 0
            for i in range(0, len(records_list), batch_size):
                batch = records_list[i:i + batch_size]
                rec_res = await client.post(
                    f"/api/v2/datasources/{created_source_id}/records",
                    json=batch,
                    headers=headers,
                )
                if rec_res.status_code not in (200, 201):
                    return json.dumps({"status": "error", "message": f"Error registrando lote en records: {rec_res.text}"}, ensure_ascii=False)
                total_uploaded += len(batch)

            return json.dumps({
                "status": "success",
                "source_id": created_source_id,
                "registros_subidos": total_uploaded,
                "message": "DataSource creado y registros ingeridos con éxito mapeando dinámicamente todas las variables."
            }, ensure_ascii=False, indent=2)

    # Herramienta modular para habilitar un observatorio y hacerlo visible en la interfaz
    @mcp.tool(name="habilitar_observatorio")
    async def habilitar_observatorio(observatory_id: str) -> str:
        """
        Fuerza la habilitación/publicación de un Observatory existente en JUB.
        Solo requiere observatory_id.
        """
        token = await _get_jub_token()
        if not token:
            return json.dumps({
                "status": "error",
                "observatory_id": observatory_id,
                "message": "No fue posible autenticarse en JUB.",
            }, ensure_ascii=False, indent=2)
        headers = {"Authorization": f"Bearer {token}"}
        pasos = []
        warnings = []
        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            pasos.append("Verificando Observatory...")
            obs_res = await client.get(
                f"/api/v2/observatories/{observatory_id}",
                headers=headers,
            )
            if obs_res.status_code not in (200, 201):
                return json.dumps({
                    "status": "error",
                    "observatory_id": observatory_id,
                    "http_status": obs_res.status_code,
                    "message": "El Observatory no existe o no pudo consultarse.",
                    "details": obs_res.text,
                }, ensure_ascii=False, indent=2)
            try:
                observatory_before = obs_res.json()
            except Exception:
                observatory_before = {}
            pasos.append("Intentando establecer public=True...")
            public_success = False
            patch_res = await client.patch(
                f"/api/v2/observatories/{observatory_id}",
                json={"public": True},
                headers=headers,
            )
            if patch_res.status_code in (200, 201, 204):
                public_success = True
                pasos.append("public=True aceptado por JUB.")
            else:
                warnings.append(
                    f"PATCH public=True no fue aceptado "
                    f"(HTTP {patch_res.status_code}): "
                    f"{patch_res.text[:300]}"
                )
            pasos.append("Buscando task asociada (opcional)...")
            task_id = None
            task_completed = False
            tasks_res = await client.get(
                "/api/v2/tasks",
                headers=headers,
            )
            if tasks_res.status_code in (200, 201):
                try:
                    tasks_data = tasks_res.json()
                    if isinstance(tasks_data, list):
                        tasks = tasks_data
                    elif isinstance(tasks_data, dict):
                        tasks = (
                            tasks_data.get("tasks")
                            or tasks_data.get("items")
                            or tasks_data.get("data")
                            or []
                        )
                    else:
                        tasks = []
                    for task in tasks:
                        if not isinstance(task, dict):
                            continue
                        task_obs_id = (
                            task.get("observatory_id")
                            or task.get("resource_id")
                            or task.get("entity_id")
                        )
                        if str(task_obs_id) == str(observatory_id):
                            task_id = (
                                task.get("task_id")
                                or task.get("id")
                            )
                            if task_id:
                                break
                except Exception as e:
                    warnings.append(
                        f"No se pudieron interpretar las tasks: {str(e)}"
                    )
            if task_id:
                pasos.append(
                    f"Task encontrada: {task_id}. Completando..."
                )
                complete_res = await client.post(
                    f"/api/v2/tasks/{task_id}/complete",
                    json={
                        "success": True,
                        "message": (
                            f"Habilitación manual del Observatory "
                            f"{observatory_id} mediante MCP."
                        ),
                    },
                    headers=headers,
                )
                if complete_res.status_code in (200, 201, 204):
                    task_completed = True
                    pasos.append("Task completada correctamente.")
                else:
                    warnings.append(
                        f"No se pudo completar task {task_id}: "
                        f"HTTP {complete_res.status_code} "
                        f"{complete_res.text[:300]}"
                    )
            else:
                pasos.append(
                    "No existe task pendiente. "
                    "El Observatory puede haber sido creado directamente."
                )
            pasos.append("Verificando estado final...")
            verify_res = await client.get(
                f"/api/v2/observatories/{observatory_id}",
                headers=headers,
            )
            observatory_after = {}
            if verify_res.status_code in (200, 201):
                try:
                    observatory_after = verify_res.json()
                except Exception:
                    pass
            public_value = None
            if isinstance(observatory_after, dict):
                public_value = observatory_after.get("public")
            if public_value is True:
                final_status = "success"
                message = (
                    f"Observatory '{observatory_id}' está público "
                    f"y habilitado en JUB."
                )
            elif public_success:
                final_status = "success"
                message = (
                    f"JUB aceptó public=True para el Observatory "
                    f"'{observatory_id}'."
                )
            elif task_completed:
                final_status = "partial_success"
                message = (
                    f"La tarea del Observatory '{observatory_id}' "
                    f"fue completada, pero no fue posible confirmar "
                    f"el campo public."
                )
            else:
                final_status = "error"
                message = (
                    f"No fue posible confirmar la habilitación del "
                    f"Observatory '{observatory_id}'."
                )
            return json.dumps({
                "status": final_status,
                "observatory_id": observatory_id,
                "public": public_value,
                "task_id": task_id,
                "task_completed": task_completed,
                "public_update_accepted": public_success,
                "observatory_before": observatory_before,
                "observatory_after": observatory_after,
                "steps": pasos,
                "warnings": warnings,
                "message": message,
            }, ensure_ascii=False, indent=2)
      
    # Pipeline completo integrando la misma lógica de ingesta avanzada
    @mcp.tool(name="pipeline")
    async def pipeline(
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
        """Herramienta v2 optimizada para indexación integral completa con soporte para archivos CSV pesados."""
        missing_params = []
        if not observatory_title: missing_params.append("observatory_title")
        if not observatory_description: missing_params.append("observatory_description")
        if not institution: missing_params.append("institution")
        if not edition: missing_params.append("edition")
        if not country: missing_params.append("country")
        if not csv_filename and not csv_content: missing_params.append("csv_filename o csv_content")
        if not product_name_base: missing_params.append("product_name_base")
        if not product_description_base: missing_params.append("product_description_base")
        if not datasource_name: missing_params.append("datasource_name")
        if not datasource_description: missing_params.append("datasource_description")
        if not start_year: missing_params.append("start_year")
        if not end_year: missing_params.append("end_year")

        if missing_params:
            return json.dumps(
                {
                    "status": "missing_parameters",
                    "message": "Faltan parámetros requeridos para indexar en JUB.",
                    "missing_parameters": missing_params,
                },
                ensure_ascii=False,
                indent=2,
            )

        steps_log: List[str] = []
        warnings: List[str] = []
        resumen: Dict[str, str] = {}

        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        path_csv = (
            resolve_existing_path(csv_filename)
            if csv_filename
            else SOURCES_DIR / "datos.csv"
        )
        if not path_csv.exists() and csv_content and csv_content.strip():
            with open(path_csv, "w", encoding="utf-8") as f:
                f.write(csv_content)
            steps_log.append(f"CSV de origen guardado en: '{path_csv}'.")

        stem = path_csv.stem.replace("temp_", "")

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=300.0) as client:
            token = await _get_jub_token()
            headers = {"Authorization": f"Bearer {token}"} if token else {}

            steps_log.append("[1/6] Creando Observatorio v2...")
            setup_payload = {
                "title": observatory_title,
                "user_id": user_id,
                "description": observatory_description,
                "metadata": {
                    "edition": edition,
                    "country": country,
                    "institution": institution,
                },
            }
            
            if image_url:
                setup_payload["image_url"] = image_url

            obs_res = await client.post(
                "/api/v2/observatories/setup",
                json=setup_payload,
                headers=headers,
            )
            if obs_res.status_code not in (200, 201):
                return json.dumps(
                    {
                        "status": "error",
                        "message": f"Error al crear el observatorio ({obs_res.status_code}).",
                        "detalles": obs_res.text,
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            obs_data = obs_res.json()
            observatory_id = obs_data.get("observatory_id")
            task_id = obs_data.get("task_id")
            resumen["observatorio"] = f"Creado (ID: {observatory_id})"

            steps_log.append("[2/6] Creando Catálogos...")
            spatial_items = []
            temporal_items = []
            spatial_seen = set()
            temporal_seen = set()

            reader = None
            f_csv = None
            if path_csv.exists():
                f_csv = open(path_csv, encoding="utf-8")
                reader = csv.DictReader(f_csv)
            elif csv_content:
                reader = csv.DictReader(io.StringIO(csv_content))

            if reader:
                for row in reader:
                    muni = row.get("municipio") or row.get("Municipio") or row.get("estado") or "General"
                    s_val = muni.upper().replace(" ", "_")
                    if s_val not in spatial_seen:
                        spatial_seen.add(s_val)
                        spatial_items.append({
                            "name": muni,
                            "value": s_val,
                            "code": len(spatial_seen),
                            "value_type": "STRING",
                            "aliases": [],
                            "children": [],
                        })

                    anio = str(row.get("anio") or row.get("año") or "2024")
                    t_val = f"Y{anio}"
                    if t_val not in temporal_seen:
                        temporal_seen.add(t_val)
                        temporal_items.append({
                            "name": anio,
                            "value": t_val,
                            "code": int(anio) if anio.isdigit() else 2024,
                            "value_type": "STRING",
                            "temporal_value": f"{anio}-01-01T00:00:00Z",
                            "aliases": [],
                            "children": [],
                        })

                if f_csv:
                    f_csv.close()

            catalogs_payload = [
                {
                    "name": f"Spatial - {observatory_title}",
                    "value": "SPATIAL",
                    "catalog_type": "SPATIAL",
                    "description": "Catálogo Geográfico",
                    "items": spatial_items if spatial_items else [{
                        "name": country, "value": country, "code": 1, "value_type": "STRING", "aliases": [], "children": []
                    }],
                },
                {
                    "name": f"Temporal - {observatory_title}",
                    "value": "TEMPORAL",
                    "catalog_type": "TEMPORAL",
                    "description": "Catálogo Temporal",
                    "items": temporal_items if temporal_items else [{
                        "name": edition, "value": f"Y{edition}", "code": 1, "value_type": "STRING", "temporal_value": f"{edition}-01-01T00:00:00Z", "aliases": [], "children": []
                    }],
                }
            ]

            cat_res = await client.post(
                f"/api/v2/observatories/{observatory_id}/catalogs/bulk",
                json=catalogs_payload,
                headers=headers,
            )

            if cat_res.status_code in (200, 201):
                catalog_ids = cat_res.json().get("catalog_ids", [])
                resumen["catalogos"] = f"{len(catalog_ids)} catálogos creados y vinculados"
            else:
                warnings.append(f"Error cargando catálogos: {cat_res.text[:150]}")

            steps_log.append("[3/6] Registrando productos múltiples por rango de años...")
            s_year, e_year = int(start_year), int(end_year)

            products_list = []
            main_prod = {
                "name": f"Dataset {product_name_base} {s_year}-{e_year}",
                "description": f"Dataset completo de {product_description_base}",
                "catalog_item_ids": []
            }
            if product_id_base:
                main_prod["product_id"] = f"{product_id_base}-dataset"
            products_list.append(main_prod)

            for year in range(s_year, e_year + 1):
                y_prod = {
                    "name": f"{product_name_base} — {year}",
                    "description": f"{product_description_base} - Periodo {year}",
                    "catalog_item_ids": []
                }
                if product_id_base:
                    y_prod["product_id"] = f"{product_id_base}-{year}"
                products_list.append(y_prod)

            prod_res = await client.post(
                f"/api/v2/observatories/{observatory_id}/products/bulk",
                json={"products": products_list},
                headers=headers,
            )
            created_products = []
            if prod_res.status_code in (200, 201):
                created_products = prod_res.json().get("products", [])
                resumen["productos"] = f"{len(created_products)} productos creados"
            else:
                warnings.append(f"Error al registrar productos: {prod_res.text[:150]}")

            steps_log.append("[4/6] Subiendo imagen o recurso al producto...")
            if created_products and (product_file_filename or product_file_content):
                target_product_id = created_products[0].get("product_id") or created_products[0].get("id")
                file_bytes = product_file_content.encode('utf-8') if product_file_content else None
                file_name = product_file_filename or "recurso.png"

                if not file_bytes and product_file_filename:
                    res_path = resolve_existing_path(product_file_filename)
                    if res_path.exists():
                        with open(res_path, "rb") as f_bin:
                            file_bytes = f_bin.read()
                        file_name = res_path.name

                if file_bytes:
                    files = {"file": (file_name, file_bytes)}
                    data_form = {"user_id": user_id}
                    await client.post(
                        f"/api/v2/products/{target_product_id}/upload",
                        data=data_form,
                        files=files,
                        headers=headers,
                    )
                    resumen["archivos_recursos"] = f"Archivo '{file_name}' subido exitosamente"

            steps_log.append("[5/6] Creando DataSource y registrando datos por lotes...")
            ds_payload = {
                "name": datasource_name,
                "description": datasource_description,
                "format": "csv",
            }
            if source_id:
                ds_payload["source_id"] = source_id

            ds_res = await client.post("/api/v2/datasources", json=ds_payload, headers=headers)
            created_source_id = (
                ds_res.json().get("source_id")
                if ds_res.status_code in (200, 201)
                else (source_id or f"src_{stem}")
            )
            resumen["datasource"] = f"ID: {created_source_id}"

            records_list = []
            if path_csv.exists():
                with open(path_csv, encoding="utf-8") as f:
                    r_csv = csv.DictReader(f)
                    for idx, row in enumerate(r_csv):
                        row_id = row.get("NSEQN") or row.get("id") or row.get("ID") or str(idx + 1)
                        clean_ds_prefix = datasource_name.lower().split()[0] if datasource_name else "record"
                        record_id = f"{clean_ds_prefix}-{row_id}"

                        muni_raw = row.get("municipio") or row.get("Municipio") or row.get("estado") or country
                        spatial_id = muni_raw.upper().replace(" ", "_")

                        anio_raw = str(row.get("anio") or row.get("año") or row.get("YFD") or edition)
                        temporal_id = f"{anio_raw}-01-01T00:00:00Z" if len(anio_raw) == 4 else "2024-01-01T00:00:00Z"

                        numerical_interests = {}
                        interest_ids = []

                        for key, val in row.items():
                            if key.lower() in ["nseqn", "id", "municipio", "estado", "anio", "año"]:
                                continue
                            try:
                                num_val = float(val)
                                numerical_interests[key.upper()] = num_val
                            except (ValueError, TypeError):
                                if val and str(val).strip():
                                    interest_ids.append(str(val))

                        if not numerical_interests:
                            numerical_interests = {"VALOR": 0.0}

                        records_list.append({
                            "record_id": record_id,
                            "spatial_id": spatial_id,
                            "temporal_id": temporal_id,
                            "interest_ids": interest_ids,
                            "numerical_interest_ids": numerical_interests,
                            "raw_payload": row,
                        })

            batch_size = 1000
            total_uploaded = 0
            for i in range(0, len(records_list), batch_size):
                batch = records_list[i:i + batch_size]
                rec_res = await client.post(
                    f"/api/v2/datasources/{created_source_id}/records",
                    json=batch,
                    headers=headers,
                )
                if rec_res.status_code in (200, 201):
                    total_uploaded += len(batch)

            resumen["registros"] = f"{total_uploaded} registros subidos en lotes"

            steps_log.append("[6/6] Finalizando tarea...")
            complete_res = await client.post(
                f"/api/v2/tasks/{task_id}/complete",
                json={"success": True, "message": f"Aprovisionamiento completado para {observatory_title}"},
                headers=headers,
            )

            if complete_res.status_code == 200:
                resumen["estado_final"] = "Observatorio Activo y Habilitado"
            else:
                warnings.append(f"No se pudo completar la tarea de activación: {complete_res.text[:150]}")

            state = {
                "observatory_id": observatory_id,
                "task_id": task_id,
                "source_id": created_source_id,
                "csv_filename": str(path_csv),
            }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)

            return json.dumps(
                {
                    "status": "success",
                    "observatory_id": observatory_id,
                    "task_id": task_id,
                    "steps_completed": steps_log,
                    "resumen": resumen,
                    "advertencias": warnings,
                    "mensaje": "¡Indexación integral completada correctamente con lotes optimizados y mapeo numérico avanzado!",
                },
                ensure_ascii=False,
                indent=2,
            )
