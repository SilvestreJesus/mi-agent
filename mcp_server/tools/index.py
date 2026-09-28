import csv
import io
import json
import os
import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastmcp import FastMCP

from config import JUB_URL, JUB_USER, JUB_PASS, DATA_RECORDS_FILE, STATE_FILE


SOURCES_DIR = Path("sources")
IMAGES_DIR = Path("images")
DATA_DIR = Path("data")

OLLAMA_URL_INTERNO = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "llava")


# =============================================================================
# Helpers
# =============================================================================

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


def _json_response(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text


def _load_state() -> Dict[str, Any]:
    path = Path(STATE_FILE)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(**updates: Any) -> Dict[str, Any]:
    state = _load_state()
    state.update({k: v for k, v in updates.items() if v is not None})

    path = Path(STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    return state


def _normalize_value(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .upper()
        .replace("Á", "A")
        .replace("É", "E")
        .replace("Í", "I")
        .replace("Ó", "O")
        .replace("Ú", "U")
        .replace("Ü", "U")
        .replace("Ñ", "N")
        .replace(" ", "_")
        .replace("-", "_")
    )


def _read_csv_rows(
    csv_filename: Optional[str] = None,
    csv_content: Optional[str] = None,
) -> Tuple[List[Dict[str, str]], Optional[Path]]:
    if csv_filename:
        path = resolve_existing_path(csv_filename)
        if path.exists():
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                return list(csv.DictReader(f)), path

    if csv_content and csv_content.strip():
        return list(csv.DictReader(io.StringIO(csv_content))), None

    return [], None


def _pick(row: Dict[str, Any], *names: str) -> Optional[str]:
    """Busca una columna ignorando mayúsculas/minúsculas y espacios."""
    normalized = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        value = normalized.get(name.strip().lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _build_catalogs_payload(
    observatory_title: str,
    country: str,
    edition: str,
    rows: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """
    Construye los catálogos que este MCP puede inferir con seguridad:
      - SPATIAL
      - TEMPORAL

    IMPORTANTE:
    Los catálogos se crean Y enlazan después mediante:
      POST /api/v2/catalogs/bulk/{observatory_id}/link

    No se usa /observatories/{id}/catalogs/bulk para enviar definiciones
    completas de catálogo.
    """
    spatial_items: List[Dict[str, Any]] = []
    temporal_items: List[Dict[str, Any]] = []
    spatial_seen = set()
    temporal_seen = set()

    for row in rows:
        place = (
            _pick(row, "municipio", "Municipio", "estado", "Estado", "region", "región")
            or country
        )
        spatial_value = _normalize_value(place)

        if spatial_value and spatial_value not in spatial_seen:
            spatial_seen.add(spatial_value)
            spatial_items.append(
                {
                    "name": place,
                    "value": spatial_value,
                    "code": len(spatial_seen),
                    "value_type": "STRING",
                    "description": f"Ubicación espacial: {place}",
                    "aliases": [],
                    "children": [],
                }
            )

        year = _pick(row, "anio", "año", "year", "YFD") or str(edition)
        year = str(year).strip()
        temporal_value = f"Y{year}"

        if temporal_value not in temporal_seen:
            temporal_seen.add(temporal_value)
            temporal_items.append(
                {
                    "name": year,
                    "value": temporal_value,
                    "code": int(year) if year.isdigit() else len(temporal_seen),
                    "value_type": "STRING",
                    "temporal_value": (
                        f"{year}-01-01T00:00:00Z"
                        if len(year) == 4 and year.isdigit()
                        else None
                    ),
                    "description": f"Periodo temporal: {year}",
                    "aliases": [],
                    "children": [],
                }
            )

    if not spatial_items:
        spatial_items = [
            {
                "name": country,
                "value": _normalize_value(country),
                "code": 1,
                "value_type": "STRING",
                "description": f"Ubicación espacial base: {country}",
                "aliases": [],
                "children": [],
            }
        ]

    if not temporal_items:
        temporal_items = [
            {
                "name": str(edition),
                "value": f"Y{edition}",
                "code": int(edition) if str(edition).isdigit() else 1,
                "value_type": "STRING",
                "temporal_value": f"{edition}-01-01T00:00:00Z",
                "description": f"Periodo temporal base: {edition}",
                "aliases": [],
                "children": [],
            }
        ]

    # Quitar temporal_value=None porque algunos validadores Pydantic lo rechazan.
    for item in temporal_items:
        if item.get("temporal_value") is None:
            item.pop("temporal_value", None)

    safe_title = _normalize_value(observatory_title)[:40] or "OBSERVATORY"

    return [
        {
            "catalog_id": f"cat_spatial_{safe_title.lower()}",
            "name": f"Spatial - {observatory_title}",
            "value": "SPATIAL",
            "catalog_type": "SPATIAL",
            "description": "Catálogo geográfico del observatorio.",
            "items": spatial_items,
        },
        {
            "catalog_id": f"cat_temporal_{safe_title.lower()}",
            "name": f"Temporal - {observatory_title}",
            "value": "TEMPORAL",
            "catalog_type": "TEMPORAL",
            "description": "Catálogo temporal del observatorio.",
            "items": temporal_items,
        },
    ]


async def _get_jub_token() -> Optional[str]:
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


async def _headers() -> Dict[str, str]:
    token = await _get_jub_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _setup_observatory(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    observatory_title: str,
    observatory_description: str,
    institution: str,
    edition: str,
    country: str,
    image_url: Optional[str],
    user_id: str,
    observatory_id: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "title": observatory_title,
        "user_id": user_id,
        "description": observatory_description,
        "metadata": {
            "edition": str(edition),
            "country": country,
            "institution": institution,
        },
    }

    if image_url:
        payload["image_url"] = image_url
    if observatory_id:
        payload["observatory_id"] = observatory_id

    res = await client.post(
        "/api/v2/observatories/setup",
        json=payload,
        headers=headers,
    )

    if res.status_code not in (200, 201):
        raise RuntimeError(
            f"setup observatory HTTP {res.status_code}: {res.text}"
        )

    data = res.json()
    obs_id = data.get("observatory_id")
    task_id = data.get("task_id")

    if not obs_id:
        raise RuntimeError(f"JUB no devolvió observatory_id: {data}")
    if not task_id:
        raise RuntimeError(f"JUB no devolvió task_id: {data}")

    _save_state(
        observatory_id=obs_id,
        task_id=task_id,
        observatory_title=observatory_title,
        edition=str(edition),
        country=country,
    )

    return data


async def _create_and_link_catalogs(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    observatory_id: str,
    observatory_title: str,
    country: str,
    edition: str,
    csv_filename: Optional[str] = None,
    csv_content: Optional[str] = None,
) -> Dict[str, Any]:
    """
    CORRECCIÓN CLAVE.

    La API documentada por el usuario expone:
      POST /api/v2/catalogs/bulk/{observatory_id}/link

    Este endpoint crea los catálogos en bulk y los enlaza al mismo
    Observatory en una sola operación.

    El código anterior enviaba definiciones completas a:
      /api/v2/observatories/{observatory_id}/catalogs/bulk

    que corresponde a la colección de asignación del Observatory y era
    la causa de que los catálogos no quedaran asociados como se esperaba.
    """
    rows, path = _read_csv_rows(csv_filename, csv_content)
    catalogs_payload = _build_catalogs_payload(
        observatory_title=observatory_title,
        country=country,
        edition=str(edition),
        rows=rows,
    )

    res = await client.post(
        f"/api/v2/catalogs/bulk/{observatory_id}/link",
        json=catalogs_payload,
        headers=headers,
    )

    if res.status_code not in (200, 201):
        raise RuntimeError(
            "Error creando/enlazando catálogos. "
            f"HTTP {res.status_code}: {res.text}"
        )

    response_data = _json_response(res)

    _save_state(
        observatory_id=observatory_id,
        csv_filename=str(path) if path else csv_filename,
        catalogs_created=True,
    )

    return {
        "payload": catalogs_payload,
        "response": response_data,
        "catalog_count": len(catalogs_payload),
        "spatial_items": len(catalogs_payload[0]["items"]),
        "temporal_items": len(catalogs_payload[1]["items"]),
    }


async def _create_products(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    observatory_id: str,
    product_name_base: str,
    product_description_base: str,
    product_id_base: Optional[str],
    start_year: str,
    end_year: str,
) -> Dict[str, Any]:
    try:
        s_year = int(start_year)
        e_year = int(end_year)
    except (TypeError, ValueError):
        raise RuntimeError("start_year y end_year deben ser numéricos.")

    if s_year > e_year:
        raise RuntimeError("start_year no puede ser mayor que end_year.")

    products: List[Dict[str, Any]] = []

    main_product: Dict[str, Any] = {
        "name": f"Dataset {product_name_base} {s_year}-{e_year}",
        "description": f"Dataset completo de {product_description_base}",
        "catalog_item_ids": [],
    }
    if product_id_base:
        main_product["product_id"] = f"{product_id_base}-dataset"
    products.append(main_product)

    for year in range(s_year, e_year + 1):
        product: Dict[str, Any] = {
            "name": f"{product_name_base} — {year}",
            "description": f"{product_description_base} - Periodo {year}",
            "catalog_item_ids": [],
        }
        if product_id_base:
            product["product_id"] = f"{product_id_base}-{year}"
        products.append(product)

    res = await client.post(
        f"/api/v2/observatories/{observatory_id}/products/bulk",
        json={"products": products},
        headers=headers,
    )

    if res.status_code not in (200, 201):
        raise RuntimeError(
            f"Error creando productos HTTP {res.status_code}: {res.text}"
        )

    return {
        "products_sent": products,
        "response": _json_response(res),
        "count": len(products),
    }


async def _complete_observatory_task(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    task_id: str,
    observatory_title: str,
) -> Dict[str, Any]:
    res = await client.post(
        f"/api/v2/tasks/{task_id}/complete",
        json={
            "success": True,
            "message": (
                f"Catálogos y productos aprovisionados correctamente "
                f"para {observatory_title}."
            ),
        },
        headers=headers,
    )

    if res.status_code not in (200, 201):
        raise RuntimeError(
            f"No se pudo habilitar el observatorio. "
            f"HTTP {res.status_code}: {res.text}"
        )

    _save_state(observatory_enabled=True)
    return {
        "status_code": res.status_code,
        "response": _json_response(res),
    }


async def _create_datasource_and_records(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    datasource_name: str,
    datasource_description: str,
    csv_filename: Optional[str],
    csv_content: Optional[str],
    source_id: Optional[str],
    edition: str,
    country: str,
) -> Dict[str, Any]:
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)

    path_csv = (
        resolve_existing_path(csv_filename)
        if csv_filename
        else SOURCES_DIR / "datos.csv"
    )

    if not path_csv.exists() and csv_content and csv_content.strip():
        with open(path_csv, "w", encoding="utf-8") as f:
            f.write(csv_content)

    if not path_csv.exists():
        raise RuntimeError(
            f"No se encontró el CSV para ingesta: {path_csv}"
        )

    ds_payload: Dict[str, Any] = {
        "name": datasource_name,
        "description": datasource_description,
        "format": "csv",
    }

    # El esquema compartido de /api/v2/datasources no incluye source_id.
    # No se envía source_id en el body; JUB lo genera.
    ds_res = await client.post(
        "/api/v2/datasources",
        json=ds_payload,
        headers=headers,
    )

    if ds_res.status_code not in (200, 201):
        raise RuntimeError(
            f"Error creando DataSource HTTP {ds_res.status_code}: {ds_res.text}"
        )

    ds_data = ds_res.json()
    created_source_id = ds_data.get("source_id")
    if not created_source_id:
        raise RuntimeError(f"JUB no devolvió source_id: {ds_data}")

    records: List[Dict[str, Any]] = []

    with open(path_csv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        for idx, row in enumerate(reader):
            prefix = (
                _normalize_value(datasource_name).lower().split("_")[0]
                if datasource_name
                else "record"
            )
            record_id = f"{prefix}-{idx + 1}"

            place = (
                _pick(row, "municipio", "Municipio", "estado", "Estado")
                or country
            )

            year = (
                _pick(row, "anio", "año", "year", "YFD")
                or str(edition)
            )

            numerical_interests: Dict[str, float] = {}
            interest_ids: List[str] = []

            ignored = {
                "nseqn", "id", "municipio", "estado",
                "anio", "año", "year", "yfd",
            }

            for key, value in row.items():
                if str(key).strip().lower() in ignored:
                    continue
                if value is None or not str(value).strip():
                    continue

                try:
                    numerical_interests[_normalize_value(key)] = float(value)
                except (TypeError, ValueError):
                    # Se conserva el comportamiento previo.
                    # Para interest_ids reales se requiere resolver cada value
                    # contra catalog_item_id de JUB.
                    interest_ids.append(str(value).strip())

            if not numerical_interests:
                numerical_interests = {"VALOR": 0.0}

            records.append(
                {
                    "record_id": record_id,
                    "spatial_id": _normalize_value(place),
                    "temporal_id": (
                        f"{year}-01-01T00:00:00Z"
                        if len(str(year)) == 4 and str(year).isdigit()
                        else f"{edition}-01-01T00:00:00Z"
                    ),
                    "interest_ids": interest_ids,
                    "numerical_interest_ids": numerical_interests,
                    "raw_payload": row,
                }
            )

    batch_size = 1000
    total_uploaded = 0

    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        rec_res = await client.post(
            f"/api/v2/datasources/{created_source_id}/records",
            json=batch,
            headers=headers,
        )

        if rec_res.status_code not in (200, 201):
            raise RuntimeError(
                f"Error registrando records HTTP {rec_res.status_code}: "
                f"{rec_res.text}"
            )

        total_uploaded += len(batch)

    _save_state(
        source_id=created_source_id,
        csv_filename=str(path_csv),
    )

    return {
        "source_id": created_source_id,
        "records_uploaded": total_uploaded,
    }


# =============================================================================
# MCP
# =============================================================================

def register(mcp: FastMCP):

    @mcp.tool(name="analizar_imagen_con_ia")
    async def analizar_imagen_con_ia(
        url_imagen: str,
        pregunta_o_instruccion: str,
    ) -> str:
        """Analiza una imagen remota usando el modelo multimodal configurado."""
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
                "stream": False,
            }

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{OLLAMA_URL_INTERNO}/api/generate",
                    json=payload,
                )
                response.raise_for_status()

            data = response.json()
            return json.dumps(
                {
                    "status": "success",
                    "url": url_imagen,
                    "analisis": data.get("response", "No se obtuvo respuesta."),
                },
                ensure_ascii=False,
                indent=2,
            )

        except Exception as e:
            return json.dumps(
                {"status": "error", "message": str(e)},
                ensure_ascii=False,
                indent=2,
            )


    # -------------------------------------------------------------------------
    # 1. Observatory
    # -------------------------------------------------------------------------

    @mcp.tool(name="crear_observatorio")
    async def crear_observatorio(
        observatory_title: str,
        observatory_description: str,
        institution: str,
        edition: str,
        country: str,
        image_url: Optional[str] = None,
        user_id: str = "usr_system",
        observatory_id: Optional[str] = None,
    ) -> str:
        """
        Crea el Observatory con /setup.

        IMPORTANTE:
        NO completa la task aquí.
        El Observatory queda pendiente/deshabilitado hasta que
        crear_productos() termine correctamente.
        """
        try:
            headers = await _headers()

            async with httpx.AsyncClient(
                base_url=JUB_URL,
                timeout=60.0,
            ) as client:
                data = await _setup_observatory(
                    client=client,
                    headers=headers,
                    observatory_title=observatory_title,
                    observatory_description=observatory_description,
                    institution=institution,
                    edition=edition,
                    country=country,
                    image_url=image_url,
                    user_id=user_id,
                    observatory_id=observatory_id,
                )

            return json.dumps(
                {
                    "status": "success",
                    "observatory_id": data.get("observatory_id"),
                    "task_id": data.get("task_id"),
                    "provisioning_status": data.get("status", "pending"),
                    "message": (
                        "Observatorio creado correctamente en estado PENDING. "
                        "Ahora ejecuta crear_catalogos y después crear_productos. "
                        "crear_productos habilitará el observatorio."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )

        except Exception as e:
            return json.dumps(
                {"status": "error", "step": "crear_observatorio", "message": str(e)},
                ensure_ascii=False,
                indent=2,
            )


    # -------------------------------------------------------------------------
    # 2. Catalogs
    # -------------------------------------------------------------------------

    @mcp.tool(name="crear_catalogos")
    async def crear_catalogos(
        observatory_id: Optional[str] = None,
        observatory_title: Optional[str] = None,
        country: Optional[str] = None,
        edition: Optional[str] = None,
        csv_filename: Optional[str] = None,
        csv_content: Optional[str] = None,
    ) -> str:
        """
        Crea los catálogos SPATIAL y TEMPORAL y LOS ENLAZA al mismo
        Observatory usando:

          POST /api/v2/catalogs/bulk/{observatory_id}/link

        Si faltan observatory_id/title/country/edition se recuperan
        automáticamente de STATE_FILE.
        """
        try:
            state = _load_state()

            observatory_id = observatory_id or state.get("observatory_id")
            observatory_title = (
                observatory_title
                or state.get("observatory_title")
                or "Observatorio"
            )
            country = country or state.get("country") or "México"
            edition = str(edition or state.get("edition") or "2024")

            if not observatory_id:
                raise RuntimeError(
                    "No hay observatory_id. Ejecuta crear_observatorio primero "
                    "o proporciona observatory_id."
                )

            headers = await _headers()

            async with httpx.AsyncClient(
                base_url=JUB_URL,
                timeout=120.0,
            ) as client:
                result = await _create_and_link_catalogs(
                    client=client,
                    headers=headers,
                    observatory_id=observatory_id,
                    observatory_title=observatory_title,
                    country=country,
                    edition=edition,
                    csv_filename=csv_filename,
                    csv_content=csv_content,
                )

            return json.dumps(
                {
                    "status": "success",
                    "observatory_id": observatory_id,
                    "catalogos_creados_y_enlazados": result["catalog_count"],
                    "spatial_items": result["spatial_items"],
                    "temporal_items": result["temporal_items"],
                    "jub_response": result["response"],
                    "message": (
                        "Catálogos creados y enlazados al mismo Observatory "
                        "correctamente."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )

        except Exception as e:
            return json.dumps(
                {
                    "status": "error",
                    "step": "crear_catalogos",
                    "message": str(e),
                },
                ensure_ascii=False,
                indent=2,
            )


    # -------------------------------------------------------------------------
    # 3. Products + ACTIVACIÓN
    # -------------------------------------------------------------------------

    @mcp.tool(name="crear_productos")
    async def crear_productos(
        observatory_id: Optional[str] = None,
        product_name_base: str = "Producto",
        product_desc_base: Optional[str] = None,
        product_description_base: Optional[str] = None,
        product_id_base: Optional[str] = None,
        start_year: str = "2000",
        end_year: str = "2026",
        task_id: Optional[str] = None,
        observatory_title: Optional[str] = None,
    ) -> str:
        """
        Crea y asigna productos al Observatory.

        CAMBIO SOLICITADO:
        Si los productos se crean correctamente, esta misma herramienta
        completa /tasks/{task_id}/complete y habilita el Observatory.
        """
        try:
            state = _load_state()

            observatory_id = observatory_id or state.get("observatory_id")
            task_id = task_id or state.get("task_id")
            observatory_title = (
                observatory_title
                or state.get("observatory_title")
                or "Observatorio"
            )

            if not observatory_id:
                raise RuntimeError(
                    "No hay observatory_id. Ejecuta crear_observatorio primero."
                )
            if not task_id:
                raise RuntimeError(
                    "No hay task_id. crear_observatorio debe ejecutarse con "
                    "/api/v2/observatories/setup antes de crear_productos."
                )

            description = (
                product_desc_base
                or product_description_base
                or product_name_base
            )

            headers = await _headers()

            async with httpx.AsyncClient(
                base_url=JUB_URL,
                timeout=120.0,
            ) as client:
                product_result = await _create_products(
                    client=client,
                    headers=headers,
                    observatory_id=observatory_id,
                    product_name_base=product_name_base,
                    product_description_base=description,
                    product_id_base=product_id_base,
                    start_year=start_year,
                    end_year=end_year,
                )

                # SOLO se habilita si productos terminó correctamente.
                activation_result = await _complete_observatory_task(
                    client=client,
                    headers=headers,
                    task_id=task_id,
                    observatory_title=observatory_title,
                )

            return json.dumps(
                {
                    "status": "success",
                    "observatory_id": observatory_id,
                    "task_id": task_id,
                    "productos_creados": product_result["count"],
                    "products_response": product_result["response"],
                    "activation_response": activation_result["response"],
                    "observatory_enabled": True,
                    "message": (
                        "Productos creados correctamente. La tarea fue completada "
                        "y el Observatory quedó habilitado."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )

        except Exception as e:
            return json.dumps(
                {
                    "status": "error",
                    "step": "crear_productos_o_habilitar",
                    "message": str(e),
                    "observatory_enabled": False,
                },
                ensure_ascii=False,
                indent=2,
            )


    # -------------------------------------------------------------------------
    # 4. DataSource + Records
    # -------------------------------------------------------------------------

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
        try:
            headers = await _headers()

            async with httpx.AsyncClient(
                base_url=JUB_URL,
                timeout=300.0,
            ) as client:
                result = await _create_datasource_and_records(
                    client=client,
                    headers=headers,
                    datasource_name=datasource_name,
                    datasource_description=datasource_description,
                    csv_filename=csv_filename,
                    csv_content=csv_content,
                    source_id=source_id,
                    edition=edition,
                    country=country,
                )

            return json.dumps(
                {
                    "status": "success",
                    "source_id": result["source_id"],
                    "registros_subidos": result["records_uploaded"],
                    "message": "DataSource creado y registros ingeridos.",
                },
                ensure_ascii=False,
                indent=2,
            )

        except Exception as e:
            return json.dumps(
                {
                    "status": "error",
                    "step": "crear_datasource_y_ingestar",
                    "message": str(e),
                },
                ensure_ascii=False,
                indent=2,
            )


    # -------------------------------------------------------------------------
    # 5. Pipeline completo
    # -------------------------------------------------------------------------

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
        observatory_id: Optional[str] = None,
    ) -> str:
        """
        Pipeline JUB v2:

          1) setup Observatory (PENDING)
          2) crear + enlazar catálogos al MISMO Observatory
          3) crear + asignar productos
          4) COMPLETE TASK -> Observatory ENABLED
          5) recurso opcional del producto
          6) DataSource + records

        La activación ocurre inmediatamente DESPUÉS de productos.
        """
        required = {
            "observatory_title": observatory_title,
            "observatory_description": observatory_description,
            "institution": institution,
            "edition": edition,
            "country": country,
            "product_name_base": product_name_base,
            "product_description_base": product_description_base,
            "start_year": start_year,
            "end_year": end_year,
            "datasource_name": datasource_name,
            "datasource_description": datasource_description,
        }

        missing = [name for name, value in required.items() if not value]

        if not csv_filename and not csv_content:
            missing.append("csv_filename o csv_content")

        if missing:
            return json.dumps(
                {
                    "status": "missing_parameters",
                    "message": "Faltan parámetros requeridos.",
                    "missing_parameters": missing,
                },
                ensure_ascii=False,
                indent=2,
            )

        steps: List[str] = []
        warnings: List[str] = []
        summary: Dict[str, Any] = {}

        try:
            SOURCES_DIR.mkdir(parents=True, exist_ok=True)
            IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            DATA_DIR.mkdir(parents=True, exist_ok=True)

            # Si llega csv_content, persistirlo para que también pueda usarlo
            # la fase de DataSource.
            effective_csv_filename = csv_filename
            if not effective_csv_filename and csv_content:
                path_csv = SOURCES_DIR / "datos_pipeline.csv"
                with open(path_csv, "w", encoding="utf-8") as f:
                    f.write(csv_content)
                effective_csv_filename = str(path_csv)

            headers = await _headers()

            async with httpx.AsyncClient(
                base_url=JUB_URL,
                timeout=300.0,
            ) as client:

                # -------------------------------------------------------------
                # 1/6 Observatory setup
                # -------------------------------------------------------------
                steps.append("[1/6] Creando Observatory con /setup...")
                obs_data = await _setup_observatory(
                    client=client,
                    headers=headers,
                    observatory_title=observatory_title,
                    observatory_description=observatory_description,
                    institution=institution,
                    edition=str(edition),
                    country=country,
                    image_url=image_url,
                    user_id=user_id,
                    observatory_id=observatory_id,
                )

                created_observatory_id = obs_data["observatory_id"]
                task_id = obs_data["task_id"]

                summary["observatory"] = {
                    "observatory_id": created_observatory_id,
                    "task_id": task_id,
                    "status": "pending",
                }

                # -------------------------------------------------------------
                # 2/6 Catalogs: create + link
                # -------------------------------------------------------------
                steps.append(
                    "[2/6] Creando y enlazando catálogos al Observatory..."
                )

                catalog_result = await _create_and_link_catalogs(
                    client=client,
                    headers=headers,
                    observatory_id=created_observatory_id,
                    observatory_title=observatory_title,
                    country=country,
                    edition=str(edition),
                    csv_filename=effective_csv_filename,
                    csv_content=csv_content,
                )

                summary["catalogs"] = {
                    "count": catalog_result["catalog_count"],
                    "spatial_items": catalog_result["spatial_items"],
                    "temporal_items": catalog_result["temporal_items"],
                }

                # -------------------------------------------------------------
                # 3/6 Products
                # -------------------------------------------------------------
                steps.append(
                    "[3/6] Creando y asignando productos al Observatory..."
                )

                product_result = await _create_products(
                    client=client,
                    headers=headers,
                    observatory_id=created_observatory_id,
                    product_name_base=product_name_base,
                    product_description_base=product_description_base,
                    product_id_base=product_id_base,
                    start_year=str(start_year),
                    end_year=str(end_year),
                )

                summary["products"] = {
                    "count": product_result["count"],
                    "response": product_result["response"],
                }

                # -------------------------------------------------------------
                # 4/6 ACTIVACIÓN justo después de productos
                # -------------------------------------------------------------
                steps.append(
                    "[4/6] Completando task y habilitando Observatory..."
                )

                activation_result = await _complete_observatory_task(
                    client=client,
                    headers=headers,
                    task_id=task_id,
                    observatory_title=observatory_title,
                )

                summary["observatory"]["status"] = "enabled"
                summary["activation"] = activation_result["response"]

                # -------------------------------------------------------------
                # Recurso opcional del primer producto
                # -------------------------------------------------------------
                if product_file_filename or product_file_content:
                    product_response = product_result["response"]
                    created_products = []

                    if isinstance(product_response, dict):
                        created_products = product_response.get("products", [])

                    if created_products:
                        target_product_id = (
                            created_products[0].get("product_id")
                            or created_products[0].get("id")
                        )

                        file_bytes = (
                            product_file_content.encode("utf-8")
                            if product_file_content
                            else None
                        )
                        file_name = product_file_filename or "recurso.txt"

                        if not file_bytes and product_file_filename:
                            resource_path = resolve_existing_path(
                                product_file_filename
                            )
                            if resource_path.exists():
                                with open(resource_path, "rb") as f:
                                    file_bytes = f.read()
                                file_name = resource_path.name

                        if target_product_id and file_bytes:
                            upload_res = await client.post(
                                f"/api/v2/products/{target_product_id}/upload",
                                data={"user_id": user_id},
                                files={"file": (file_name, file_bytes)},
                                headers=headers,
                            )

                            if upload_res.status_code not in (200, 201):
                                warnings.append(
                                    "Observatory ya fue habilitado, pero falló "
                                    f"el recurso del producto: {upload_res.text}"
                                )
                            else:
                                summary["product_resource"] = file_name
                    else:
                        warnings.append(
                            "JUB no devolvió la lista de productos; "
                            "se omitió la carga del recurso."
                        )

                # -------------------------------------------------------------
                # 5/6 DataSource + records
                # -------------------------------------------------------------
                steps.append("[5/6] Creando DataSource e ingestando records...")

                datasource_result = await _create_datasource_and_records(
                    client=client,
                    headers=headers,
                    datasource_name=datasource_name,
                    datasource_description=datasource_description,
                    csv_filename=effective_csv_filename,
                    csv_content=csv_content,
                    source_id=source_id,
                    edition=str(edition),
                    country=country,
                )

                summary["datasource"] = datasource_result

                # -------------------------------------------------------------
                # 6/6 Estado final
                # -------------------------------------------------------------
                steps.append("[6/6] Pipeline finalizado.")

                _save_state(
                    observatory_id=created_observatory_id,
                    task_id=task_id,
                    source_id=datasource_result["source_id"],
                    observatory_enabled=True,
                    catalogs_created=True,
                    products_created=True,
                    csv_filename=effective_csv_filename,
                )

            return json.dumps(
                {
                    "status": "success",
                    "observatory_id": created_observatory_id,
                    "task_id": task_id,
                    "observatory_enabled": True,
                    "steps_completed": steps,
                    "summary": summary,
                    "warnings": warnings,
                    "message": (
                        "Pipeline completado. Catálogos y productos quedaron "
                        "asociados al mismo Observatory y la activación ocurrió "
                        "después de crear los productos."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )

        except Exception as e:
            return json.dumps(
                {
                    "status": "error",
                    "message": str(e),
                    "steps_completed": steps,
                    "warnings": warnings,
                    "summary": summary,
                    "important": (
                        "Si el error ocurre antes de [4/6], el Observatory "
                        "permanece deshabilitado y la task NO se completa."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
