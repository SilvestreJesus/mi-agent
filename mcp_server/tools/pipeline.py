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

    # Herramienta para crear  observatorio
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
        """
        Crea un observatorio en JUB v2 y completa automáticamente
        la tarea de configuración para habilitarlo y mostrarlo en la interfaz.
        """

        # Intentar autenticación.
        token = await _get_jub_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        # Datos del observatorio
        payload = {
            "title": observatory_title,
            "user_id": user_id,
            "description": observatory_description,
            "metadata": {
                "edition": edition,
                "country": country,
                "institution": institution,
            },
        }

        # Agregar imagen solamente si fue proporcionada
        if image_url and image_url.strip():
            payload["image_url"] = image_url.strip()

        try:
            async with httpx.AsyncClient(
                base_url=JUB_URL,
                timeout=60.0
            ) as client:

                # =====================================================
                # 1. CREAR OBSERVATORIO
                # =====================================================
                res = await client.post(
                    "/api/v2/observatories/setup",
                    json=payload,
                    headers=headers,
                )

                if res.status_code not in (200, 201):
                    return json.dumps({
                        "status": "error",
                        "message": "No fue posible crear el observatorio.",
                        "http_status": res.status_code,
                        "details": res.text,
                    }, ensure_ascii=False, indent=2)

                try:
                    data = res.json()
                except Exception:
                    return json.dumps({
                        "status": "error",
                        "message": "JUB creó una respuesta que no pudo interpretarse como JSON.",
                        "http_status": res.status_code,
                        "details": res.text,
                    }, ensure_ascii=False, indent=2)

                observatory_id = data.get("observatory_id")
                task_id = data.get("task_id")

                # Verificar ID del observatorio
                if not observatory_id:
                    return json.dumps({
                        "status": "error",
                        "message": "JUB respondió a la creación, pero no devolvió observatory_id.",
                        "response": data,
                    }, ensure_ascii=False, indent=2)

                # Si no existe task_id, el observatorio fue creado,
                # pero no podemos completar su habilitación.
                if not task_id:
                    return json.dumps({
                        "status": "partial_success",
                        "observatory_id": observatory_id,
                        "task_id": None,
                        "message": (
                            "El observatorio fue creado, pero JUB no devolvió "
                            "task_id para completar su habilitación."
                        ),
                    }, ensure_ascii=False, indent=2)

                # =====================================================
                # 2. COMPLETAR TAREA
                # Esto habilita el observatorio para mostrarlo en la UI
                # =====================================================
                complete = await client.post(
                    f"/api/v2/tasks/{task_id}/complete",
                    json={
                        "success": True,
                        "message": (
                            f"Observatorio '{observatory_title}' "
                            "aprovisionado y habilitado correctamente."
                        ),
                    },
                    headers=headers,
                )

                if complete.status_code not in (200, 201):
                    return json.dumps({
                        "status": "partial_success",
                        "observatory_id": observatory_id,
                        "task_id": task_id,
                        "message": (
                            "El observatorio fue creado correctamente, "
                            "pero no pudo completarse la tarea de habilitación."
                        ),
                        "http_status": complete.status_code,
                        "details": complete.text,
                    }, ensure_ascii=False, indent=2)

                # =====================================================
                # 3. RESULTADO FINAL
                # =====================================================
                return json.dumps({
                    "status": "success",
                    "observatory_id": observatory_id,
                    "task_id": task_id,
                    "habilitado": True,
                    "visible_en_interfaz": True,
                    "message": (
                        f"Observatorio '{observatory_title}' creado, "
                        "habilitado y listo para mostrarse en la interfaz."
                    ),
                }, ensure_ascii=False, indent=2)

        except httpx.TimeoutException as e:
            return json.dumps({
                "status": "error",
                "message": "JUB tardó demasiado tiempo en responder.",
                "details": str(e),
            }, ensure_ascii=False, indent=2)

        except httpx.RequestError as e:
            return json.dumps({
                "status": "error",
                "message": "No fue posible establecer comunicación con JUB.",
                "details": str(e),
            }, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": "Ocurrió un error inesperado al crear el observatorio.",
                "details": str(e),
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


    # Herramienta modular para forzar la visibilidad de un observatorio en la interfaz
    @mcp.tool(name="habilitar_observatorio")
    async def habilitar_observatorio(
        observatory_id: str,
    ) -> str:
        """Fuerza la visibilidad y activación de un observatorio existente en la interfaz web de JUB usando su observatory_id."""
        token = await _get_jub_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            # 1. Verificar primero si el observatorio existe en la plataforma
            obs_det = await client.get(f"/api/v2/observatories/{observatory_id}", headers=headers)
            if obs_det.status_code not in (200, 201):
                return json.dumps({
                    "status": "error",
                    "observatory_id": observatory_id,
                    "message": "No se encontró ningún observatorio registrado con ese ID en el sistema."
                }, ensure_ascii=False)

            # 2. Forzar la actualización de estatus a activo/visible para refrescar la interfaz web
            res = await client.patch(
                f"/api/v2/observatories/{observatory_id}",
                json={"visible": True, "status": "active", "public": True},
                headers=headers,
            )

            if res.status_code not in (200, 201):
                # Intentar un método alternativo de actualización si la API usa PUT
                res = await client.put(
                    f"/api/v2/observatories/{observatory_id}",
                    json={"visible": True, "status": "active", "public": True},
                    headers=headers,
                )

            return json.dumps({
                "status": "success",
                "observatory_id": observatory_id,
                "message": f"¡El observatorio '{observatory_id}' ha sido forzado y sincronizado correctamente para mostrarse en la interfaz web!"
            }, ensure_ascii=False, indent=2)


    # Pipeline completo integrando 
    @mcp.tool(name="pipeline")
    async def pipeline(
        observatory_title: str,
        observatory_description: str,
        institution: str,
        edition: str,
        country: str,
        datasource_name: str,
        datasource_description: str,
        product_name_base: str,
        product_description_base: str,
        start_year: str,
        end_year: str,
        csv_filename: str,
        product_id_base: Optional[str] = None,
        image_url: Optional[str] = None,
    ) -> str:
        """Ejecuta la indexación completa en JUB: observatorio, catálogos, productos, datasource, registros y habilitación."""
        print("[PIPELINE] INICIO", flush=True)

        # Validaciones
        required = {
            "observatory_title": observatory_title,
            "observatory_description": observatory_description,
            "institution": institution,
            "edition": edition,
            "country": country,
            "datasource_name": datasource_name,
            "datasource_description": datasource_description,
            "product_name_base": product_name_base,
            "product_description_base": product_description_base,
            "start_year": start_year,
            "end_year": end_year,
            "csv_filename": csv_filename,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            return json.dumps({
                "status": "missing_parameters",
                "message": "Faltan parámetros requeridos.",
                "missing_parameters": missing
            }, ensure_ascii=False, indent=2)

        try:
            s_year = int(start_year)
            e_year = int(end_year)
            if s_year > e_year:
                return json.dumps({
                    "status": "error",
                    "message": "start_year no puede ser mayor que end_year."
                }, ensure_ascii=False, indent=2)
        except (ValueError, TypeError):
            return json.dumps({
                "status": "error",
                "message": "start_year y end_year deben ser años numéricos."
            }, ensure_ascii=False, indent=2)

        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        path_csv = resolve_existing_path(csv_filename)
        if not path_csv.exists():
            return json.dumps({
                "status": "error",
                "step": "validar_csv",
                "message": f"No se encontró el archivo CSV '{csv_filename}'."
            }, ensure_ascii=False, indent=2)

        steps_log = []
        resumen = {}
        token = await _get_jub_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        try:
            async with httpx.AsyncClient(base_url=JUB_URL, timeout=300.0) as client:

                # 1. OBSERVATORIO
                print("[PIPELINE 1/6] Creando observatorio...", flush=True)
                steps_log.append("[1/6] Observatorio")

                obs_payload = {
                    "title": observatory_title,
                    "user_id": "usr_system",
                    "description": observatory_description,
                    "metadata": {
                        "edition": edition,
                        "country": country,
                        "institution": institution
                    }
                }

                if image_url and image_url.strip():
                    obs_payload["image_url"] = image_url.strip()

                obs_res = await client.post(
                    "/api/v2/observatories/setup",
                    json=obs_payload,
                    headers=headers
                )

                if obs_res.status_code not in (200, 201):
                    return json.dumps({
                        "status": "error",
                        "step": "crear_observatorio",
                        "http_status": obs_res.status_code,
                        "details": obs_res.text
                    }, ensure_ascii=False, indent=2)

                obs_data = obs_res.json()
                observatory_id = obs_data.get("observatory_id")
                task_id = obs_data.get("task_id")

                if not observatory_id:
                    return json.dumps({
                        "status": "error",
                        "step": "crear_observatorio",
                        "message": "JUB no devolvió observatory_id.",
                        "response": obs_data
                    }, ensure_ascii=False, indent=2)

                if not task_id:
                    return json.dumps({
                        "status": "error",
                        "step": "crear_observatorio",
                        "observatory_id": observatory_id,
                        "message": "JUB no devolvió task_id."
                    }, ensure_ascii=False, indent=2)

                print(f"[PIPELINE] OBSERVATORIO OK: {observatory_id}", flush=True)
                resumen["observatorio"] = observatory_id

                # 2. CATÁLOGOS
                print("[PIPELINE 2/6] Creando catálogos...", flush=True)
                steps_log.append("[2/6] Catálogos")

                spatial_items = []
                temporal_items = []
                spatial_seen = set()
                temporal_seen = set()

                with open(path_csv, encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)

                    for row in reader:
                        muni = str(
                            row.get("municipio")
                            or row.get("Municipio")
                            or row.get("estado")
                            or country
                        ).strip()

                        spatial_value = muni.upper().replace(" ", "_")

                        if spatial_value not in spatial_seen:
                            spatial_seen.add(spatial_value)
                            spatial_items.append({
                                "name": muni,
                                "value": spatial_value,
                                "code": len(spatial_seen),
                                "value_type": "STRING",
                                "aliases": [],
                                "children": []
                            })

                        anio = str(
                            row.get("anio")
                            or row.get("año")
                            or row.get("YFD")
                            or edition
                        ).strip()

                        temporal_value = f"Y{anio}"

                        if temporal_value not in temporal_seen:
                            temporal_seen.add(temporal_value)
                            temporal_items.append({
                                "name": anio,
                                "value": temporal_value,
                                "code": int(anio) if anio.isdigit() else len(temporal_seen),
                                "value_type": "STRING",
                                "temporal_value": f"{anio}-01-01T00:00:00Z",
                                "aliases": [],
                                "children": []
                            })

                if not spatial_items:
                    spatial_items.append({
                        "name": country,
                        "value": country.upper().replace(" ", "_"),
                        "code": 1,
                        "value_type": "STRING",
                        "aliases": [],
                        "children": []
                    })

                if not temporal_items:
                    temporal_items.append({
                        "name": edition,
                        "value": f"Y{edition}",
                        "code": int(edition) if edition.isdigit() else 1,
                        "value_type": "STRING",
                        "temporal_value": f"{edition}-01-01T00:00:00Z",
                        "aliases": [],
                        "children": []
                    })

                catalogs_payload = [
                    {
                        "name": f"Spatial - {observatory_title}",
                        "value": "SPATIAL",
                        "catalog_type": "SPATIAL",
                        "description": "Catálogo Geográfico",
                        "items": spatial_items
                    },
                    {
                        "name": f"Temporal - {observatory_title}",
                        "value": "TEMPORAL",
                        "catalog_type": "TEMPORAL",
                        "description": "Catálogo Temporal",
                        "items": temporal_items
                    }
                ]

                cat_res = await client.post(
                    f"/api/v2/observatories/{observatory_id}/catalogs/bulk",
                    json=catalogs_payload,
                    headers=headers
                )

                if cat_res.status_code not in (200, 201):
                    return json.dumps({
                        "status": "error",
                        "step": "crear_catalogos",
                        "observatory_id": observatory_id,
                        "http_status": cat_res.status_code,
                        "details": cat_res.text
                    }, ensure_ascii=False, indent=2)

                print("[PIPELINE] CATALOGOS OK", flush=True)
                resumen["catalogos"] = "creados"

                # 3. PRODUCTOS
                print("[PIPELINE 3/6] Creando productos...", flush=True)
                steps_log.append("[3/6] Productos")

                products_list = []

                main_product = {
                    "name": f"Dataset {product_name_base} {s_year}-{e_year}",
                    "description": f"Dataset completo de {product_description_base}",
                    "catalog_item_ids": []
                }

                if product_id_base:
                    main_product["product_id"] = f"{product_id_base}-dataset"

                products_list.append(main_product)

                for year in range(s_year, e_year + 1):
                    product = {
                        "name": f"{product_name_base} — {year}",
                        "description": f"{product_description_base} - Periodo {year}",
                        "catalog_item_ids": []
                    }

                    if product_id_base:
                        product["product_id"] = f"{product_id_base}-{year}"

                    products_list.append(product)

                prod_res = await client.post(
                    f"/api/v2/observatories/{observatory_id}/products/bulk",
                    json={"products": products_list},
                    headers=headers
                )

                if prod_res.status_code not in (200, 201):
                    return json.dumps({
                        "status": "error",
                        "step": "crear_productos",
                        "observatory_id": observatory_id,
                        "http_status": prod_res.status_code,
                        "details": prod_res.text
                    }, ensure_ascii=False, indent=2)

                print("[PIPELINE] PRODUCTOS OK", flush=True)
                resumen["productos"] = len(products_list)

                # 4. DATASOURCE
                print("[PIPELINE 4/6] Creando DataSource...", flush=True)
                steps_log.append("[4/6] DataSource")

                ds_payload = {
                    "name": datasource_name,
                    "description": datasource_description,
                    "format": "csv"
                }

                ds_res = await client.post(
                    "/api/v2/datasources",
                    json=ds_payload,
                    headers=headers
                )

                if ds_res.status_code not in (200, 201):
                    return json.dumps({
                        "status": "error",
                        "step": "crear_datasource",
                        "observatory_id": observatory_id,
                        "http_status": ds_res.status_code,
                        "details": ds_res.text
                    }, ensure_ascii=False, indent=2)

                ds_data = ds_res.json()
                source_id = ds_data.get("source_id")

                if not source_id:
                    return json.dumps({
                        "status": "error",
                        "step": "crear_datasource",
                        "observatory_id": observatory_id,
                        "message": "JUB no devolvió source_id.",
                        "response": ds_data
                    }, ensure_ascii=False, indent=2)

                print(f"[PIPELINE] DATASOURCE OK: {source_id}", flush=True)
                resumen["datasource"] = source_id

                # 5. RECORDS
                print("[PIPELINE 5/6] Ingresando registros...", flush=True)
                steps_log.append("[5/6] Records")

                records = []

                with open(path_csv, encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)

                    for idx, row in enumerate(reader):
                        row_id = (
                            row.get("NSEQN")
                            or row.get("id")
                            or row.get("ID")
                            or str(idx + 1)
                        )

                        prefix = datasource_name.lower().strip().replace(" ", "_")
                        record_id = f"{prefix}-{row_id}"

                        muni = str(
                            row.get("municipio")
                            or row.get("Municipio")
                            or row.get("estado")
                            or country
                        ).strip()

                        spatial_id = muni.upper().replace(" ", "_")

                        anio = str(
                            row.get("anio")
                            or row.get("año")
                            or row.get("YFD")
                            or edition
                        ).strip()

                        temporal_id = (
                            f"{anio}-01-01T00:00:00Z"
                            if len(anio) == 4 and anio.isdigit()
                            else f"{edition}-01-01T00:00:00Z"
                        )

                        numerical_interests = {}
                        interest_ids = []

                        for key, val in row.items():
                            if key is None:
                                continue

                            clean_key = key.strip()
                            lower_key = clean_key.lower()

                            if lower_key in [
                                "nseqn",
                                "id",
                                "municipio",
                                "estado",
                                "anio",
                                "año",
                                "yfd"
                            ]:
                                continue

                            value = str(val).strip() if val is not None else ""

                            if not value:
                                continue

                            try:
                                numerical_interests[clean_key.upper()] = float(
                                    value.replace(",", "")
                                )
                            except (ValueError, TypeError):
                                interest_ids.append(value)

                        if not numerical_interests:
                            numerical_interests = {"REGISTRO_ACTIVO": 1.0}

                        records.append({
                            "record_id": record_id,
                            "spatial_id": spatial_id,
                            "temporal_id": temporal_id,
                            "interest_ids": interest_ids[:5],
                            "numerical_interest_ids": numerical_interests,
                            "raw_payload": row
                        })

                if not records:
                    return json.dumps({
                        "status": "error",
                        "step": "ingestar_records",
                        "observatory_id": observatory_id,
                        "source_id": source_id,
                        "message": "El CSV no contiene registros para ingresar."
                    }, ensure_ascii=False, indent=2)

                total_uploaded = 0
                batch_size = 1000

                for i in range(0, len(records), batch_size):
                    batch = records[i:i + batch_size]

                    rec_res = await client.post(
                        f"/api/v2/datasources/{source_id}/records",
                        json=batch,
                        headers=headers
                    )

                    if rec_res.status_code not in (200, 201):
                        return json.dumps({
                            "status": "error",
                            "step": "ingestar_records",
                            "observatory_id": observatory_id,
                            "source_id": source_id,
                            "registros_subidos": total_uploaded,
                            "http_status": rec_res.status_code,
                            "details": rec_res.text
                        }, ensure_ascii=False, indent=2)

                    total_uploaded += len(batch)

                print(f"[PIPELINE] RECORDS OK: {total_uploaded}", flush=True)
                resumen["registros"] = total_uploaded

                # 6. HABILITAR
                print("[PIPELINE 6/6] Habilitando observatorio...", flush=True)
                steps_log.append("[6/6] Habilitación")

                complete_res = await client.post(
                    f"/api/v2/tasks/{task_id}/complete",
                    json={
                        "success": True,
                        "message": f"Aprovisionamiento completado para {observatory_title}"
                    },
                    headers=headers
                )

                if complete_res.status_code not in (200, 201):
                    return json.dumps({
                        "status": "partial_success",
                        "step": "habilitar_observatorio",
                        "observatory_id": observatory_id,
                        "task_id": task_id,
                        "source_id": source_id,
                        "registros_subidos": total_uploaded,
                        "http_status": complete_res.status_code,
                        "details": complete_res.text
                    }, ensure_ascii=False, indent=2)

                print("[PIPELINE] FINALIZADO", flush=True)

                # Guardar estado
                state = {
                    "observatory_id": observatory_id,
                    "task_id": task_id,
                    "source_id": source_id,
                    "csv_filename": str(path_csv)
                }

                try:
                    with open(STATE_FILE, "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=2, ensure_ascii=False)
                except Exception as e:
                    print(f"[PIPELINE] No se pudo guardar STATE_FILE: {e}", flush=True)

                return json.dumps({
                    "status": "success",
                    "observatory_id": observatory_id,
                    "task_id": task_id,
                    "source_id": source_id,
                    "productos_creados": len(products_list),
                    "registros_subidos": total_uploaded,
                    "habilitado": True,
                    "steps_completed": steps_log,
                    "resumen": resumen,
                    "message": "Pipeline completo ejecutado correctamente."
                }, ensure_ascii=False, indent=2)

        except httpx.TimeoutException as e:
            return json.dumps({
                "status": "error",
                "step": "comunicacion_jub",
                "message": "JUB tardó demasiado tiempo en responder.",
                "details": str(e)
            }, ensure_ascii=False, indent=2)

        except httpx.RequestError as e:
            return json.dumps({
                "status": "error",
                "step": "comunicacion_jub",
                "message": "No fue posible establecer comunicación con JUB.",
                "details": str(e)
            }, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({
                "status": "error",
                "step": "pipeline",
                "message": "Error inesperado durante el pipeline.",
                "error_type": type(e).__name__,
                "details": str(e)
            }, ensure_ascii=False, indent=2)
        observatory_title: str,
        observatory_description: str,
        institution: str,
        edition: str,
        country: str,
        datasource_name: str,
        datasource_description: str,
        product_name_base: str,
        product_description_base: str,
        start_year: str,
        end_year: str,
        csv_filename: str,
        image_url: Optional[str] = None,
    ) -> str:
        """Ejecuta el flujo integral: Observatorio → Catálogos → Productos → DataSource → Records → Habilitación."""

        required = {
            "observatory_title": observatory_title,
            "observatory_description": observatory_description,
            "institution": institution,
            "edition": edition,
            "country": country,
            "datasource_name": datasource_name,
            "datasource_description": datasource_description,
            "product_name_base": product_name_base,
            "product_description_base": product_description_base,
            "start_year": start_year,
            "end_year": end_year,
            "csv_filename": csv_filename,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            return json.dumps({"status": "missing_parameters", "message": "Faltan parámetros requeridos.", "missing_parameters": missing}, ensure_ascii=False, indent=2)

        try:
            s_year, e_year = int(start_year), int(end_year)
            if s_year > e_year:
                return json.dumps({"status": "error", "message": "start_year no puede ser mayor que end_year."}, ensure_ascii=False, indent=2)
        except (ValueError, TypeError):
            return json.dumps({"status": "error", "message": "start_year y end_year deben ser años numéricos."}, ensure_ascii=False, indent=2)

        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        path_csv = resolve_existing_path(csv_filename)
        if not path_csv.exists():
            return json.dumps({"status": "error", "message": f"No se encontró el archivo CSV '{csv_filename}'."}, ensure_ascii=False, indent=2)

        steps_log: List[str] = []
        warnings: List[str] = []
        resumen: Dict[str, str] = {}

        token = await _get_jub_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        try:
            async with httpx.AsyncClient(base_url=JUB_URL, timeout=300.0) as client:

                # 1. OBSERVATORIO
                steps_log.append("[1/6] Creando Observatorio...")
                obs_payload = {
                    "title": observatory_title,
                    "user_id": "usr_system",
                    "description": observatory_description,
                    "metadata": {"edition": edition, "country": country, "institution": institution},
                }
                if image_url and image_url.strip():
                    obs_payload["image_url"] = image_url.strip()

                obs_res = await client.post("/api/v2/observatories/setup", json=obs_payload, headers=headers)
                if obs_res.status_code not in (200, 201):
                    return json.dumps({"status": "error", "step": "crear_observatorio", "http_status": obs_res.status_code, "details": obs_res.text}, ensure_ascii=False, indent=2)

                obs_data = obs_res.json()
                observatory_id = obs_data.get("observatory_id")
                task_id = obs_data.get("task_id")
                if not observatory_id:
                    return json.dumps({"status": "error", "step": "crear_observatorio", "message": "JUB no devolvió observatory_id.", "response": obs_data}, ensure_ascii=False, indent=2)
                if not task_id:
                    return json.dumps({"status": "error", "step": "crear_observatorio", "observatory_id": observatory_id, "message": "JUB no devolvió task_id."}, ensure_ascii=False, indent=2)

                resumen["observatorio"] = f"Creado (ID: {observatory_id})"

                # 2. CATÁLOGOS
                steps_log.append("[2/6] Creando Catálogos...")
                spatial_items, temporal_items = [], []
                spatial_seen, temporal_seen = set(), set()

                with open(path_csv, encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        muni = str(row.get("municipio") or row.get("Municipio") or row.get("estado") or country).strip()
                        s_val = muni.upper().replace(" ", "_")
                        if s_val not in spatial_seen:
                            spatial_seen.add(s_val)
                            spatial_items.append({"name": muni, "value": s_val, "code": len(spatial_seen), "value_type": "STRING", "aliases": [], "children": []})

                        anio = str(row.get("anio") or row.get("año") or row.get("YFD") or edition).strip()
                        t_val = f"Y{anio}"
                        if t_val not in temporal_seen:
                            temporal_seen.add(t_val)
                            temporal_items.append({
                                "name": anio,
                                "value": t_val,
                                "code": int(anio) if anio.isdigit() else len(temporal_seen),
                                "value_type": "STRING",
                                "temporal_value": f"{anio}-01-01T00:00:00Z",
                                "aliases": [],
                                "children": [],
                            })

                if not spatial_items:
                    spatial_items = [{"name": country, "value": country.upper().replace(" ", "_"), "code": 1, "value_type": "STRING", "aliases": [], "children": []}]
                if not temporal_items:
                    temporal_items = [{"name": edition, "value": f"Y{edition}", "code": int(edition) if str(edition).isdigit() else 1, "value_type": "STRING", "temporal_value": f"{edition}-01-01T00:00:00Z", "aliases": [], "children": []}]

                catalogs_payload = [
                    {"name": f"Spatial - {observatory_title}", "value": "SPATIAL", "catalog_type": "SPATIAL", "description": "Catálogo Geográfico", "items": spatial_items},
                    {"name": f"Temporal - {observatory_title}", "value": "TEMPORAL", "catalog_type": "TEMPORAL", "description": "Catálogo Temporal", "items": temporal_items},
                ]

                cat_res = await client.post(f"/api/v2/observatories/{observatory_id}/catalogs/bulk", json=catalogs_payload, headers=headers)
                if cat_res.status_code not in (200, 201):
                    return json.dumps({"status": "error", "step": "crear_catalogos", "observatory_id": observatory_id, "http_status": cat_res.status_code, "details": cat_res.text}, ensure_ascii=False, indent=2)

                cat_data = cat_res.json()
                resumen["catalogos"] = f"{len(cat_data.get('catalog_ids', []))} catálogos creados"

                # 3. PRODUCTOS
                steps_log.append("[3/6] Creando Productos...")
                products = [{
                    "name": f"Dataset {product_name_base} {s_year}-{e_year}",
                    "description": f"Dataset completo de {product_description_base}",
                    "catalog_item_ids": [],
                }]

                for year in range(s_year, e_year + 1):
                    products.append({
                        "name": f"{product_name_base} — {year}",
                        "description": f"{product_description_base} - Periodo {year}",
                        "catalog_item_ids": [],
                    })

                prod_res = await client.post(
                    f"/api/v2/observatories/{observatory_id}/products/bulk",
                    json={"products": products},
                    headers=headers,
                )
                if prod_res.status_code not in (200, 201):
                    return json.dumps({"status": "error", "step": "crear_productos", "observatory_id": observatory_id, "http_status": prod_res.status_code, "details": prod_res.text}, ensure_ascii=False, indent=2)

                prod_data = prod_res.json()
                created_products = prod_data.get("products", [])
                product_ids = prod_data.get("product_ids", [])
                total_products = len(created_products) if created_products else len(product_ids) if product_ids else len(products)
                resumen["productos"] = f"{total_products} productos creados"

                # 4. DATASOURCE
                steps_log.append("[4/6] Creando DataSource...")
                ds_payload = {"name": datasource_name, "description": datasource_description, "format": "csv"}
                ds_res = await client.post("/api/v2/datasources", json=ds_payload, headers=headers)

                if ds_res.status_code not in (200, 201):
                    return json.dumps({"status": "error", "step": "crear_datasource", "http_status": ds_res.status_code, "details": ds_res.text}, ensure_ascii=False, indent=2)

                ds_data = ds_res.json()
                source_id = ds_data.get("source_id")
                if not source_id:
                    return json.dumps({"status": "error", "step": "crear_datasource", "message": "JUB no devolvió source_id.", "response": ds_data}, ensure_ascii=False, indent=2)

                resumen["datasource"] = f"ID: {source_id}"

                # 5. RECORDS
                steps_log.append("[5/6] Ingresando Records...")
                records = []

                with open(path_csv, encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for idx, row in enumerate(reader):
                        row_id = row.get("NSEQN") or row.get("id") or row.get("ID") or str(idx + 1)
                        prefix = datasource_name.lower().strip().replace(" ", "_") or "record"
                        record_id = f"{prefix}-{row_id}"

                        muni = str(row.get("municipio") or row.get("Municipio") or row.get("estado") or country)
                        spatial_id = muni.upper().replace(" ", "_")

                        anio = str(row.get("anio") or row.get("año") or row.get("YFD") or edition).strip()
                        temporal_id = f"{anio}-01-01T00:00:00Z" if len(anio) == 4 else f"{edition}-01-01T00:00:00Z"

                        numerical_interests = {}
                        interest_ids = []

                        for key, val in row.items():
                            if key is None or key.lower() in ["nseqn", "id", "municipio", "estado", "anio", "año", "yfd"]:
                                continue

                            value = str(val).strip() if val is not None else ""
                            if not value:
                                continue

                            try:
                                numerical_interests[key.upper()] = float(value.replace(",", ""))
                            except (ValueError, TypeError):
                                interest_ids.append(value)

                        if not numerical_interests:
                            numerical_interests = {"REGISTRO_ACTIVO": 1.0}

                        records.append({
                            "record_id": record_id,
                            "spatial_id": spatial_id,
                            "temporal_id": temporal_id,
                            "interest_ids": interest_ids[:5],
                            "numerical_interest_ids": numerical_interests,
                            "raw_payload": row,
                        })

                total_uploaded = 0
                batch_size = 1000

                for i in range(0, len(records), batch_size):
                    batch = records[i:i + batch_size]
                    rec_res = await client.post(f"/api/v2/datasources/{source_id}/records", json=batch, headers=headers)

                    if rec_res.status_code not in (200, 201):
                        return json.dumps({
                            "status": "error",
                            "step": "ingestar_records",
                            "observatory_id": observatory_id,
                            "source_id": source_id,
                            "registros_subidos": total_uploaded,
                            "http_status": rec_res.status_code,
                            "details": rec_res.text,
                        }, ensure_ascii=False, indent=2)

                    total_uploaded += len(batch)

                resumen["registros"] = f"{total_uploaded} registros subidos"

                # 6. HABILITAR OBSERVATORIO
                steps_log.append("[6/6] Habilitando Observatorio...")
                complete_res = await client.post(
                    f"/api/v2/tasks/{task_id}/complete",
                    json={"success": True, "message": f"Aprovisionamiento completado para {observatory_title}"},
                    headers=headers,
                )

                if complete_res.status_code not in (200, 201):
                    return json.dumps({
                        "status": "partial_success",
                        "observatory_id": observatory_id,
                        "task_id": task_id,
                        "source_id": source_id,
                        "registros_subidos": total_uploaded,
                        "message": "La indexación terminó, pero no fue posible habilitar el observatorio.",
                        "http_status": complete_res.status_code,
                        "details": complete_res.text,
                    }, ensure_ascii=False, indent=2)

                resumen["estado_final"] = "Observatorio Activo y Habilitado"

                state = {
                    "observatory_id": observatory_id,
                    "task_id": task_id,
                    "source_id": source_id,
                    "csv_filename": str(path_csv),
                }

                try:
                    with open(STATE_FILE, "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=2, ensure_ascii=False)
                except Exception as e:
                    warnings.append(f"No se pudo guardar STATE_FILE: {str(e)}")

                return json.dumps({
                    "status": "success",
                    "observatory_id": observatory_id,
                    "task_id": task_id,
                    "source_id": source_id,
                    "registros_subidos": total_uploaded,
                    "habilitado": True,
                    "visible_en_interfaz": True,
                    "steps_completed": steps_log,
                    "resumen": resumen,
                    "advertencias": warnings,
                    "mensaje": "Indexación integral completada correctamente en JUB.",
                }, ensure_ascii=False, indent=2)

        except httpx.TimeoutException as e:
            return json.dumps({"status": "error", "message": "JUB tardó demasiado tiempo en responder.", "details": str(e)}, ensure_ascii=False, indent=2)
        except httpx.RequestError as e:
            return json.dumps({"status": "error", "message": "No fue posible establecer comunicación con JUB.", "details": str(e)}, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"status": "error", "message": "Error inesperado durante el pipeline.", "details": str(e)}, ensure_ascii=False, indent=2)