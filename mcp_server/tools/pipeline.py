import csv
import io
import json
import os
import base64
import re
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastmcp import FastMCP

from config import JUB_URL, JUB_USER, JUB_PASS, STATE_FILE


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


def _is_success(response: httpx.Response) -> bool:
    return 200 <= response.status_code < 300


def _response_data(response: httpx.Response):
    try:
        return response.json()
    except Exception:
        return response.text


def _json_response(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _normalize_identifier(value: str) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_")


def _normalize_spatial(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        value = "GENERAL"
    return value.upper().replace(" ", "_").replace("-", "_")


def _normalize_year(value, default_year: str = "2024") -> str:
    value = str(value or "").strip()
    if len(value) == 4 and value.isdigit():
        return value

    default_year = str(default_year or "2024").strip()
    if len(default_year) == 4 and default_year.isdigit():
        return default_year

    return "2024"


def _to_number(value):
    if value is None:
        return None

    value = str(value).strip()
    if not value:
        return None

    try:
        return float(value.replace(",", ""))
    except (ValueError, TypeError):
        return None


async def _get_jub_token() -> Optional[str]:
    try:
        async with httpx.AsyncClient(base_url=JUB_URL, timeout=30.0) as client:
            auth_res = await client.post(
                "/api/v2/users/auth",
                json={"username": JUB_USER, "password": JUB_PASS}
            )

            if not _is_success(auth_res):
                return None

            data = auth_res.json()
            return data.get("access_token") or data.get("token")
    except Exception:
        return None


async def _get_headers():
    token = await _get_jub_token()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


def _read_csv(csv_filename: Optional[str], csv_content: Optional[str]):
    if csv_filename:
        path = resolve_existing_path(csv_filename)

        if path.exists():
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            return rows, path

    if csv_content and csv_content.strip():
        return list(csv.DictReader(io.StringIO(csv_content))), None

    return [], None


def _save_csv_content(csv_content: str, filename: str = "datos.csv") -> Path:
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    path = SOURCES_DIR / filename

    with open(path, "w", encoding="utf-8") as f:
        f.write(csv_content)

    return path


def _generate_source_id(
    source_id: Optional[str],
    path_csv: Optional[Path],
    datasource_name: str
) -> str:
    if source_id and str(source_id).strip():
        return str(source_id).strip()

    base = path_csv.stem if path_csv else datasource_name
    base = base.replace("temp_", "")
    base = _normalize_identifier(base)

    if not base:
        base = "datos"

    return f"src_{base}"


def _build_record(
    row: dict,
    index: int,
    datasource_name: str,
    edition: str,
    country: str
):
    row_id = row.get("NSEQN") or row.get("id") or row.get("ID") or str(index + 1)
    prefix = _normalize_identifier(datasource_name) or "record"
    record_id = f"{prefix}-{row_id}"

    spatial_raw = (
        row.get("municipio")
        or row.get("Municipio")
        or row.get("estado")
        or row.get("Estado")
        or country
    )
    spatial_id = _normalize_spatial(spatial_raw)

    year_raw = (
        row.get("anio")
        or row.get("año")
        or row.get("Año")
        or row.get("YFD")
        or edition
    )
    year = _normalize_year(year_raw, edition)
    temporal_id = f"{year}-01-01T00:00:00Z"

    numerical_interests = {}
    interest_ids = []
    ignored_columns = {"nseqn", "id", "municipio", "estado", "anio", "año", "yfd"}

    for key, value in row.items():
        if key is None:
            continue

        clean_key = key.strip().lower()
        if clean_key in ignored_columns:
            continue

        value_string = str(value).strip() if value is not None else ""
        if not value_string:
            continue

        numeric_value = _to_number(value_string)

        if numeric_value is not None:
            numerical_interests[key.strip().upper()] = numeric_value
        elif value_string not in interest_ids:
            interest_ids.append(value_string)

    if not numerical_interests:
        numerical_interests = {"REGISTRO_ACTIVO": 1.0}

    return {
        "record_id": record_id,
        "spatial_id": spatial_id,
        "temporal_id": temporal_id,
        "interest_ids": interest_ids[:5],
        "numerical_interest_ids": numerical_interests,
        "raw_payload": row
    }


def _build_catalogs(rows: List[dict], observatory_title: str, country: str, edition: str):
    spatial_items = []
    temporal_items = []
    spatial_seen = set()
    temporal_seen = set()

    for row in rows:
        municipality = (
            row.get("municipio")
            or row.get("Municipio")
            or row.get("estado")
            or row.get("Estado")
            or country
        )

        spatial_value = _normalize_spatial(municipality)

        if spatial_value not in spatial_seen:
            spatial_seen.add(spatial_value)
            spatial_items.append({
                "name": municipality,
                "value": spatial_value,
                "code": len(spatial_seen),
                "value_type": "STRING",
                "aliases": [],
                "children": []
            })

        year = _normalize_year(
            row.get("anio") or row.get("año") or row.get("Año") or edition,
            edition
        )
        temporal_value = f"Y{year}"

        if temporal_value not in temporal_seen:
            temporal_seen.add(temporal_value)
            temporal_items.append({
                "name": year,
                "value": temporal_value,
                "code": int(year),
                "value_type": "STRING",
                "temporal_value": f"{year}-01-01T00:00:00Z",
                "aliases": [],
                "children": []
            })

    if not spatial_items:
        spatial_items = [{
            "name": country,
            "value": _normalize_spatial(country),
            "code": 1,
            "value_type": "STRING",
            "aliases": [],
            "children": []
        }]

    if not temporal_items:
        year = _normalize_year(edition)
        temporal_items = [{
            "name": year,
            "value": f"Y{year}",
            "code": int(year),
            "value_type": "STRING",
            "temporal_value": f"{year}-01-01T00:00:00Z",
            "aliases": [],
            "children": []
        }]

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

    return catalogs_payload, spatial_items, temporal_items


def _build_products(
    product_name_base: str,
    product_description_base: str,
    start_year: int,
    end_year: int,
    product_id_base: Optional[str] = None
):
    products_list = []

    main_product = {
        "name": f"Dataset {product_name_base} {start_year}-{end_year}",
        "description": f"Dataset completo de {product_description_base}",
        "catalog_item_ids": []
    }

    if product_id_base:
        main_product["product_id"] = f"{product_id_base}-dataset"

    products_list.append(main_product)

    for year in range(start_year, end_year + 1):
        product = {
            "name": f"{product_name_base} — {year}",
            "description": f"{product_description_base} - Periodo {year}",
            "catalog_item_ids": []
        }

        if product_id_base:
            product["product_id"] = f"{product_id_base}-{year}"

        products_list.append(product)

    return products_list


def register(mcp: FastMCP):

    @mcp.tool(name="analizar_imagen_con_ia")
    async def analizar_imagen_con_ia(
        url_imagen: str,
        pregunta_o_instruccion: str
    ) -> str:
        """Analiza una imagen desde una URL utilizando el modelo multimodal configurado en Ollama."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as fetch_client:
                img_response = await fetch_client.get(url_imagen)
                img_response.raise_for_status()
                img_bytes = img_response.content

            img_base64 = base64.b64encode(img_bytes).decode("utf-8")
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

            return _json_response({
                "status": "success",
                "url": url_imagen,
                "analisis": data.get("response", "No se obtuvo respuesta.")
            })

        except Exception as e:
            return _json_response({
                "status": "error",
                "message": "Error al analizar la imagen con IA.",
                "details": str(e)
            })


    @mcp.tool(name="crear_observatorio")
    async def crear_observatorio(
        observatory_title: str,
        observatory_description: str,
        institution: str,
        edition: str,
        country: str,
        image_url: Optional[str] = None,
        user_id: str = "usr_system"
    ) -> str:
        """Crea únicamente un observatorio y completa su tarea de configuración para habilitarlo en la interfaz de JUB."""
        headers = await _get_headers()

        if headers is None:
            return _json_response({
                "status": "error",
                "step": "authentication",
                "message": "No fue posible autenticarse con JUB."
            })

        setup_payload = {
            "title": observatory_title,
            "user_id": user_id,
            "description": observatory_description,
            "metadata": {
                "edition": edition,
                "country": country,
                "institution": institution
            }
        }

        if image_url:
            setup_payload["image_url"] = image_url

        try:
            async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
                res = await client.post(
                    "/api/v2/observatories/setup",
                    json=setup_payload,
                    headers=headers
                )

                if not _is_success(res):
                    return _json_response({
                        "status": "error",
                        "step": "crear_observatorio",
                        "http_status": res.status_code,
                        "message": "JUB rechazó la creación del observatorio.",
                        "details": _response_data(res)
                    })

                data = res.json()
                observatory_id = data.get("observatory_id")
                task_id = data.get("task_id")

                if not observatory_id:
                    return _json_response({
                        "status": "error",
                        "step": "crear_observatorio",
                        "message": "JUB no devolvió observatory_id.",
                        "response": data
                    })

                if not task_id:
                    return _json_response({
                        "status": "partial_success",
                        "observatory_id": observatory_id,
                        "enabled": False,
                        "visible": False,
                        "message": "El observatorio fue creado, pero JUB no devolvió task_id. No fue posible habilitarlo.",
                        "response": data
                    })

                complete_res = await client.post(
                    f"/api/v2/tasks/{task_id}/complete",
                    json={
                        "success": True,
                        "message": f"Observatorio '{observatory_title}' creado y habilitado."
                    },
                    headers=headers
                )

                if not _is_success(complete_res):
                    return _json_response({
                        "status": "partial_success",
                        "observatory_id": observatory_id,
                        "task_id": task_id,
                        "enabled": False,
                        "visible": False,
                        "step": "activar_observatorio",
                        "http_status": complete_res.status_code,
                        "message": "El observatorio fue creado, pero JUB rechazó la activación.",
                        "details": _response_data(complete_res)
                    })

                return _json_response({
                    "status": "success",
                    "observatory_id": observatory_id,
                    "task_id": task_id,
                    "enabled": True,
                    "visible": True,
                    "message": f"Observatorio '{observatory_title}' creado, habilitado y visible correctamente."
                })

        except Exception as e:
            return _json_response({
                "status": "error",
                "step": "crear_observatorio",
                "message": "Error comunicándose con JUB.",
                "details": str(e)
            })


    @mcp.tool(name="crear_catalogos")
    async def crear_catalogos(
        observatory_id: str,
        observatory_title: Optional[str] = "Observatorio",
        country: str = "México",
        edition: str = "2024",
        csv_filename: Optional[str] = None,
        csv_content: Optional[str] = None
    ) -> str:
        """Crea los catálogos SPATIAL y TEMPORAL para un observatorio existente."""
        headers = await _get_headers()

        if headers is None:
            return _json_response({
                "status": "error",
                "message": "No fue posible autenticarse con JUB."
            })

        rows, _ = _read_csv(csv_filename, csv_content)
        catalogs_payload, spatial_items, temporal_items = _build_catalogs(
            rows,
            observatory_title or "Observatorio",
            country,
            edition
        )

        try:
            async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
                res = await client.post(
                    f"/api/v2/observatories/{observatory_id}/catalogs/bulk",
                    json=catalogs_payload,
                    headers=headers
                )

                if not _is_success(res):
                    return _json_response({
                        "status": "error",
                        "observatory_id": observatory_id,
                        "http_status": res.status_code,
                        "message": "No fue posible crear los catálogos.",
                        "details": _response_data(res)
                    })

                return _json_response({
                    "status": "success",
                    "observatory_id": observatory_id,
                    "catalogos_creados": 2,
                    "spatial_items": len(spatial_items),
                    "temporal_items": len(temporal_items),
                    "resultado": _response_data(res),
                    "message": "Catálogos creados y vinculados correctamente."
                })

        except Exception as e:
            return _json_response({
                "status": "error",
                "message": "Error creando catálogos.",
                "details": str(e)
            })


    @mcp.tool(name="crear_productos")
    async def crear_productos(
        observatory_id: str,
        product_name_base: str,
        product_desc_base: Optional[str] = None,
        product_description_base: Optional[str] = None,
        product_id_base: Optional[str] = None,
        start_year: str = "2000",
        end_year: str = "2026"
    ) -> str:
        """Crea un producto general y productos individuales por año para un observatorio existente."""
        description = product_desc_base or product_description_base or product_name_base

        try:
            s_year = int(start_year)
            e_year = int(end_year)
        except (ValueError, TypeError):
            return _json_response({
                "status": "error",
                "message": "start_year y end_year deben ser numéricos."
            })

        if s_year > e_year:
            return _json_response({
                "status": "error",
                "message": "start_year no puede ser mayor que end_year."
            })

        headers = await _get_headers()

        if headers is None:
            return _json_response({
                "status": "error",
                "message": "No fue posible autenticarse con JUB."
            })

        products_list = _build_products(
            product_name_base,
            description,
            s_year,
            e_year,
            product_id_base
        )

        try:
            async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
                res = await client.post(
                    f"/api/v2/observatories/{observatory_id}/products/bulk",
                    json={"products": products_list},
                    headers=headers
                )

                if not _is_success(res):
                    return _json_response({
                        "status": "error",
                        "observatory_id": observatory_id,
                        "http_status": res.status_code,
                        "message": "No fue posible crear los productos.",
                        "details": _response_data(res)
                    })

                return _json_response({
                    "status": "success",
                    "observatory_id": observatory_id,
                    "productos_creados": len(products_list),
                    "resultado": _response_data(res),
                    "message": "Productos creados y vinculados correctamente."
                })

        except Exception as e:
            return _json_response({
                "status": "error",
                "message": "Error creando productos.",
                "details": str(e)
            })


    @mcp.tool(name="crear_datasource_y_ingestar")
    async def crear_datasource_y_ingestar(
        datasource_name: str,
        datasource_description: str,
        csv_filename: Optional[str] = None,
        csv_content: Optional[str] = None,
        source_id: Optional[str] = None,
        edition: str = "2024",
        country: str = "México"
    ) -> str:
        """Crea un DataSource e ingesta registros. Si source_id no se proporciona, genera automáticamente src_<nombre_csv>."""
        headers = await _get_headers()

        if headers is None:
            return _json_response({
                "status": "error",
                "message": "No fue posible autenticarse con JUB."
            })

        rows, path_csv = _read_csv(csv_filename, csv_content)

        if not rows:
            return _json_response({
                "status": "error",
                "message": "No se encontraron registros válidos en el CSV."
            })

        if path_csv is None and csv_content:
            filename = Path(csv_filename).name if csv_filename else "datos.csv"
            path_csv = _save_csv_content(csv_content, filename)

        generated_source_id = _generate_source_id(
            source_id=source_id,
            path_csv=path_csv,
            datasource_name=datasource_name
        )

        ds_payload = {
            "source_id": generated_source_id,
            "name": datasource_name,
            "description": datasource_description,
            "format": "csv"
        }

        try:
            async with httpx.AsyncClient(base_url=JUB_URL, timeout=300.0) as client:
                ds_res = await client.post(
                    "/api/v2/datasources",
                    json=ds_payload,
                    headers=headers
                )

                if not _is_success(ds_res):
                    return _json_response({
                        "status": "error",
                        "step": "crear_datasource",
                        "source_id": generated_source_id,
                        "http_status": ds_res.status_code,
                        "message": "Se generó el source_id, pero JUB rechazó la creación del DataSource.",
                        "details": _response_data(ds_res)
                    })

                ds_data = ds_res.json()
                created_source_id = ds_data.get("source_id") or generated_source_id

                records_list = [
                    _build_record(
                        row=row,
                        index=index,
                        datasource_name=datasource_name,
                        edition=edition,
                        country=country
                    )
                    for index, row in enumerate(rows)
                ]

                if not records_list:
                    return _json_response({
                        "status": "error",
                        "step": "preparar_registros",
                        "source_id": created_source_id,
                        "message": "No existen registros para ingestar."
                    })

                batch_size = 1000
                total_uploaded = 0

                for i in range(0, len(records_list), batch_size):
                    batch = records_list[i:i + batch_size]

                    rec_res = await client.post(
                        f"/api/v2/datasources/{created_source_id}/records",
                        json=batch,
                        headers=headers
                    )

                    if not _is_success(rec_res):
                        return _json_response({
                            "status": "error",
                            "step": "ingestar_records",
                            "source_id": created_source_id,
                            "registros_subidos": total_uploaded,
                            "lote_fallido": i // batch_size + 1,
                            "http_status": rec_res.status_code,
                            "message": "Error registrando un lote de registros.",
                            "details": _response_data(rec_res)
                        })

                    total_uploaded += len(batch)

                return _json_response({
                    "status": "success",
                    "source_id": created_source_id,
                    "source_id_generado": generated_source_id,
                    "registros_subidos": total_uploaded,
                    "message": "DataSource creado y registros ingeridos correctamente."
                })

        except Exception as e:
            return _json_response({
                "status": "error",
                "message": "Error creando el DataSource o registrando datos.",
                "details": str(e)
            })


    @mcp.tool(name="habilitar_observatorio")
    async def habilitar_observatorio(
        observatory_id: str,
        task_id: Optional[str] = None
    ) -> str:
        """Habilita un observatorio existente. Si se conoce task_id utiliza la finalización de la tarea; de lo contrario intenta actualizar el observatorio."""
        headers = await _get_headers()

        if headers is None:
            return _json_response({
                "status": "error",
                "message": "No fue posible autenticarse con JUB."
            })

        try:
            async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
                if task_id:
                    complete_res = await client.post(
                        f"/api/v2/tasks/{task_id}/complete",
                        json={
                            "success": True,
                            "message": f"Observatorio {observatory_id} habilitado."
                        },
                        headers=headers
                    )

                    if not _is_success(complete_res):
                        return _json_response({
                            "status": "error",
                            "observatory_id": observatory_id,
                            "task_id": task_id,
                            "http_status": complete_res.status_code,
                            "message": "No fue posible completar la tarea del observatorio.",
                            "details": _response_data(complete_res)
                        })

                    return _json_response({
                        "status": "success",
                        "observatory_id": observatory_id,
                        "task_id": task_id,
                        "enabled": True,
                        "visible": True,
                        "message": "Observatorio habilitado correctamente."
                    })

                obs_res = await client.get(
                    f"/api/v2/observatories/{observatory_id}",
                    headers=headers
                )

                if not _is_success(obs_res):
                    return _json_response({
                        "status": "error",
                        "observatory_id": observatory_id,
                        "http_status": obs_res.status_code,
                        "message": "No se encontró el observatorio.",
                        "details": _response_data(obs_res)
                    })

                payload = {
                    "visible": True,
                    "status": "active",
                    "public": True
                }

                patch_res = await client.patch(
                    f"/api/v2/observatories/{observatory_id}",
                    json=payload,
                    headers=headers
                )

                if _is_success(patch_res):
                    return _json_response({
                        "status": "success",
                        "observatory_id": observatory_id,
                        "enabled": True,
                        "visible": True,
                        "message": "Observatorio habilitado correctamente."
                    })

                put_res = await client.put(
                    f"/api/v2/observatories/{observatory_id}",
                    json=payload,
                    headers=headers
                )

                if not _is_success(put_res):
                    return _json_response({
                        "status": "error",
                        "observatory_id": observatory_id,
                        "message": "El observatorio existe, pero no fue posible habilitarlo.",
                        "patch_status": patch_res.status_code,
                        "patch_details": _response_data(patch_res),
                        "put_status": put_res.status_code,
                        "put_details": _response_data(put_res)
                    })

                return _json_response({
                    "status": "success",
                    "observatory_id": observatory_id,
                    "enabled": True,
                    "visible": True,
                    "message": "Observatorio habilitado correctamente."
                })

        except Exception as e:
            return _json_response({
                "status": "error",
                "observatory_id": observatory_id,
                "message": "Error habilitando el observatorio.",
                "details": str(e)
            })


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
        datasource_name: Optional[str] = None,
        datasource_description: Optional[str] = None,
        source_id: Optional[str] = None,
        image_url: Optional[str] = None,
        user_id: str = "usr_system"
    ) -> str:
        """Ejecuta la indexación integral: observatorio, catálogos, productos, DataSource, registros y activación final del observatorio."""
        required_values = {
            "observatory_title": observatory_title,
            "observatory_description": observatory_description,
            "institution": institution,
            "edition": edition,
            "country": country,
            "product_name_base": product_name_base,
            "product_description_base": product_description_base,
            "datasource_name": datasource_name,
            "datasource_description": datasource_description,
            "start_year": start_year,
            "end_year": end_year
        }

        missing_parameters = [
            name
            for name, value in required_values.items()
            if value is None or not str(value).strip()
        ]

        if not csv_filename and not csv_content:
            missing_parameters.append("csv_filename o csv_content")

        if missing_parameters:
            return _json_response({
                "status": "missing_parameters",
                "message": "Faltan parámetros requeridos para ejecutar la indexación.",
                "missing_parameters": missing_parameters
            })

        try:
            s_year = int(start_year)
            e_year = int(end_year)
        except (ValueError, TypeError):
            return _json_response({
                "status": "error",
                "step": "validation",
                "message": "start_year y end_year deben ser numéricos."
            })

        if s_year > e_year:
            return _json_response({
                "status": "error",
                "step": "validation",
                "message": "start_year no puede ser mayor que end_year."
            })

        headers = await _get_headers()

        if headers is None:
            return _json_response({
                "status": "error",
                "step": "authentication",
                "message": "No fue posible autenticarse con JUB."
            })

        rows, path_csv = _read_csv(csv_filename, csv_content)

        if not rows:
            return _json_response({
                "status": "error",
                "step": "csv",
                "message": "El CSV no existe, está vacío o no contiene registros válidos."
            })

        if path_csv is None and csv_content:
            filename = Path(csv_filename).name if csv_filename else "datos.csv"
            path_csv = _save_csv_content(csv_content, filename)

        generated_source_id = _generate_source_id(
            source_id=source_id,
            path_csv=path_csv,
            datasource_name=datasource_name
        )

        steps_log: List[str] = []
        resumen: Dict[str, str] = {}
        observatory_id = None
        task_id = None
        created_source_id = None

        try:
            async with httpx.AsyncClient(base_url=JUB_URL, timeout=300.0) as client:
                steps_log.append("[1/6] Creando observatorio...")

                setup_payload = {
                    "title": observatory_title,
                    "user_id": user_id,
                    "description": observatory_description,
                    "metadata": {
                        "edition": edition,
                        "country": country,
                        "institution": institution
                    }
                }

                if image_url:
                    setup_payload["image_url"] = image_url

                obs_res = await client.post(
                    "/api/v2/observatories/setup",
                    json=setup_payload,
                    headers=headers
                )

                if not _is_success(obs_res):
                    return _json_response({
                        "status": "error",
                        "step": "crear_observatorio",
                        "http_status": obs_res.status_code,
                        "message": "No fue posible crear el observatorio.",
                        "details": _response_data(obs_res)
                    })

                obs_data = obs_res.json()
                observatory_id = obs_data.get("observatory_id")
                task_id = obs_data.get("task_id")

                if not observatory_id:
                    return _json_response({
                        "status": "error",
                        "step": "crear_observatorio",
                        "message": "JUB no devolvió observatory_id.",
                        "response": obs_data
                    })

                if not task_id:
                    return _json_response({
                        "status": "error",
                        "step": "crear_observatorio",
                        "observatory_id": observatory_id,
                        "message": "JUB no devolvió task_id. No puede finalizarse la indexación.",
                        "response": obs_data
                    })

                resumen["observatorio"] = f"Creado: {observatory_id}"

                steps_log.append("[2/6] Creando catálogos...")

                catalogs_payload, spatial_items, temporal_items = _build_catalogs(
                    rows,
                    observatory_title,
                    country,
                    edition
                )

                cat_res = await client.post(
                    f"/api/v2/observatories/{observatory_id}/catalogs/bulk",
                    json=catalogs_payload,
                    headers=headers
                )

                if not _is_success(cat_res):
                    return _json_response({
                        "status": "error",
                        "step": "crear_catalogos",
                        "observatory_id": observatory_id,
                        "task_id": task_id,
                        "http_status": cat_res.status_code,
                        "message": "El observatorio fue creado, pero falló la creación de catálogos. La tarea no fue completada.",
                        "details": _response_data(cat_res)
                    })

                resumen["catalogos"] = (
                    f"2 catálogos creados: {len(spatial_items)} elementos espaciales "
                    f"y {len(temporal_items)} elementos temporales"
                )

                steps_log.append("[3/6] Creando productos...")

                products_list = _build_products(
                    product_name_base,
                    product_description_base,
                    s_year,
                    e_year,
                    product_id_base
                )

                prod_res = await client.post(
                    f"/api/v2/observatories/{observatory_id}/products/bulk",
                    json={"products": products_list},
                    headers=headers
                )

                if not _is_success(prod_res):
                    return _json_response({
                        "status": "error",
                        "step": "crear_productos",
                        "observatory_id": observatory_id,
                        "task_id": task_id,
                        "http_status": prod_res.status_code,
                        "message": "Falló la creación de productos. La tarea del observatorio permanece pendiente.",
                        "details": _response_data(prod_res)
                    })

                resumen["productos"] = f"{len(products_list)} productos creados"

                steps_log.append("[4/6] Creando DataSource...")

                ds_payload = {
                    "source_id": generated_source_id,
                    "name": datasource_name,
                    "description": datasource_description,
                    "format": "csv"
                }

                ds_res = await client.post(
                    "/api/v2/datasources",
                    json=ds_payload,
                    headers=headers
                )

                if not _is_success(ds_res):
                    return _json_response({
                        "status": "error",
                        "step": "crear_datasource",
                        "observatory_id": observatory_id,
                        "task_id": task_id,
                        "source_id": generated_source_id,
                        "http_status": ds_res.status_code,
                        "message": "Se generó el source_id, pero JUB rechazó la creación del DataSource.",
                        "details": _response_data(ds_res)
                    })

                ds_data = ds_res.json()
                created_source_id = ds_data.get("source_id") or generated_source_id
                resumen["datasource"] = f"Creado: {created_source_id}"

                steps_log.append("[5/6] Ingestando registros...")

                records_list = [
                    _build_record(
                        row=row,
                        index=index,
                        datasource_name=datasource_name,
                        edition=edition,
                        country=country
                    )
                    for index, row in enumerate(rows)
                ]

                if not records_list:
                    return _json_response({
                        "status": "error",
                        "step": "preparar_registros",
                        "observatory_id": observatory_id,
                        "task_id": task_id,
                        "source_id": created_source_id,
                        "message": "No existen registros para indexar."
                    })

                batch_size = 1000
                total_uploaded = 0

                for i in range(0, len(records_list), batch_size):
                    batch = records_list[i:i + batch_size]

                    rec_res = await client.post(
                        f"/api/v2/datasources/{created_source_id}/records",
                        json=batch,
                        headers=headers
                    )

                    if not _is_success(rec_res):
                        return _json_response({
                            "status": "error",
                            "step": "ingestar_records",
                            "observatory_id": observatory_id,
                            "task_id": task_id,
                            "source_id": created_source_id,
                            "registros_subidos": total_uploaded,
                            "lote_fallido": i // batch_size + 1,
                            "http_status": rec_res.status_code,
                            "message": "Falló la ingesta de registros. La tarea del observatorio no fue completada.",
                            "details": _response_data(rec_res)
                        })

                    total_uploaded += len(batch)

                resumen["registros"] = f"{total_uploaded} registros indexados"

                steps_log.append("[6/6] Habilitando observatorio...")

                complete_res = await client.post(
                    f"/api/v2/tasks/{task_id}/complete",
                    json={
                        "success": True,
                        "message": f"Aprovisionamiento completado para {observatory_title}"
                    },
                    headers=headers
                )

                if not _is_success(complete_res):
                    return _json_response({
                        "status": "partial_success",
                        "step": "activar_observatorio",
                        "observatory_id": observatory_id,
                        "task_id": task_id,
                        "source_id": created_source_id,
                        "registros_subidos": total_uploaded,
                        "enabled": False,
                        "visible": False,
                        "http_status": complete_res.status_code,
                        "message": "La indexación terminó, pero JUB rechazó la activación del observatorio.",
                        "details": _response_data(complete_res)
                    })

                resumen["estado_final"] = "Observatorio activo y habilitado"

                try:
                    state_path = Path(STATE_FILE)
                    state_path.parent.mkdir(parents=True, exist_ok=True)

                    state = {
                        "observatory_id": observatory_id,
                        "task_id": task_id,
                        "source_id": created_source_id,
                        "csv_filename": str(path_csv) if path_csv else None
                    }

                    with open(state_path, "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass

                return _json_response({
                    "status": "success",
                    "observatory_id": observatory_id,
                    "task_id": task_id,
                    "source_id": created_source_id,
                    "enabled": True,
                    "visible": True,
                    "steps_completed": steps_log,
                    "resumen": resumen,
                    "message": "Indexación integral completada correctamente. El observatorio fue habilitado y está listo para mostrarse en JUB."
                })

        except Exception as e:
            return _json_response({
                "status": "error",
                "step": "pipeline_exception",
                "observatory_id": observatory_id,
                "task_id": task_id,
                "source_id": created_source_id or generated_source_id,
                "message": "Ocurrió un error inesperado durante la indexación.",
                "details": str(e)
            })