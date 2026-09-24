import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastmcp import FastMCP

from config import JUB_PASS, JUB_URL, JUB_USER, STATE_FILE

SOURCES_DIR = Path("sources")
IMAGES_DIR = Path("images")
DATA_DIR = Path("data")

# Definición de niveles STORI según el tipo de catálogo
CATALOG_LEVELS = {
    "SPATIAL": 0,
    "TEMPORAL": 1,
    "INTEREST": 2,
    "REFERENCE": 3,
    "OBSERVABLE": 4,
}


def resolve_existing_path(filename: str) -> Path:
    """Busca un archivo dando prioridad a fuentes e imágenes locales."""
    candidate = Path(filename)
    if candidate.exists():
        return candidate

    for folder in [
        DATA_DIR,  # Priorizamos DATA_DIR porque aquí caen los JSON convertidos
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


def _get_latest_catalog() -> Optional[Path]:
    """Busca el archivo catalogs_*.json más reciente en la carpeta data/."""
    catalogs = list(DATA_DIR.glob("catalogs_*.json"))
    if not catalogs:
        fallback = DATA_DIR / "catalogs.json"
        return fallback if fallback.exists() else None
    
    catalogs.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return catalogs[0]


def _is_conflict(status_code: int, detail: str) -> bool:
    if status_code in (403, 409):
        return True
    return any(
        k in detail.lower()
        for k in ("409", "403", "already", "duplicate", "exists", "forbidden")
    )


def _flatten_items(items: List[Dict[str, Any]]):
    """Aplana recursivamente los ítems anidados (ej. Estados -> Municipios)."""
    for item in items:
        yield item
        if "children" in item and item["children"]:
            yield from _flatten_items(item["children"])


def register(mcp: FastMCP):
    """Registra la herramienta de indexación de Observatorios en FastMCP."""

    @mcp.tool(name="indexar_observatorio")
    async def indexar_observatorio(
        observatory_id: str,
        title: str,
        description: str,
        user_id: Optional[str] = "usr_system",
        metadata: Optional[Dict[str, Any]] = None,
        image_url: Optional[str] = None,
        catalogs_filename: Optional[str] = "catalogs.json",
    ) -> str:
        """Crea o recupera el Observatorio, registra catálogos en bulk de forma tolerante a duplicados y mapea índices."""
        
        target_catalogs_path = resolve_existing_path(catalogs_filename)

        if not target_catalogs_path.exists() or catalogs_filename == "catalogs.json":
            latest_catalog = _get_latest_catalog()
            if latest_catalog:
                target_catalogs_path = latest_catalog
            else:
                return f"Error: No se encontró ningún archivo de catálogos en {DATA_DIR}."

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            headers = {}

            # 1. Autenticación 
            try:
                auth_res = await client.post(
                    "/api/v2/users/auth",
                    json={"username": JUB_USER, "password": JUB_PASS},
                )
                if auth_res.status_code in (200, 201):
                    data = auth_res.json()
                    token = (
                        data.get("access_token")
                        or data.get("token")
                        or data.get("accessToken")
                    )
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
            except Exception as e:
                print(f"Advertencia de autenticación: {e}")

            # 2. Paso 1: Crear o verificar Observatorio usando el endpoint de setup v2
            obs_payload = {
                "observatory_id": observatory_id,
                "title": title,
                "description": description,
                "user_id": user_id,
                "metadata": metadata or {},
            }
            if image_url:
                obs_payload["image_url"] = image_url

            obs_res = await client.post(
                "/api/v2/observatories/setup", json=obs_payload, headers=headers
            )

            resolved_obs_id = observatory_id
            if obs_res.status_code in (200, 201):
                res_json = obs_res.json()
                resolved_obs_id = res_json.get("observatory_id") or observatory_id
            elif _is_conflict(obs_res.status_code, obs_res.text):
                print(f"El observatorio '{observatory_id}' ya existe. Continuando con los catálogos...")
            else:
                return f"Error al crear el Observatorio ({obs_res.status_code}): {obs_res.text}"

            # 3. Paso 2 y 3: Cargar y registrar catálogos en Bulk de manera tolerante a duplicados
            with open(target_catalogs_path, encoding="utf-8") as f:
                catalogs_data = json.load(f)

            bulk_payload = (
                catalogs_data
                if isinstance(catalogs_data, list)
                else [catalogs_data]
            )

            bulk_res = await client.post(
                f"/api/v2/observatories/{resolved_obs_id}/catalogs/bulk",
                json=bulk_payload,
                headers=headers,
            )

            catalog_ids = []
            if bulk_res.status_code in (200, 201):
                bulk_json = bulk_res.json()
                catalog_ids = bulk_json.get("catalog_ids", [])
            elif _is_conflict(bulk_res.status_code, bulk_res.text):
                print("Los catálogos ya estaban registrados. Recuperando listado existente...")
            else:
                return f"Error en la ingesta bulk de catálogos ({bulk_res.status_code}): {bulk_res.text}"

            # Si la lista de IDs vino vacía por conflicto previo, los consultamos directamente del observatorio
            if not catalog_ids:
                get_cats = await client.get(f"/api/v2/observatories/{resolved_obs_id}/catalogs", headers=headers)
                if get_cats.status_code == 200:
                    existing_cats = get_cats.json()
                    catalog_ids = [c.get("catalog_id") or c.get("id") for c in existing_cats if c.get("catalog_id") or c.get("id")]

            # Asegurar niveles explícitos si procede
            for cat_dict, cat_id in zip(bulk_payload, catalog_ids):
                cat_type = cat_dict.get("catalog_type", "INTEREST")
                level = CATALOG_LEVELS.get(cat_type, 2)

                await client.post(
                    f"/api/v2/observatories/{resolved_obs_id}/catalogs",
                    json={"catalog_id": cat_id, "level": level},
                    headers=headers,
                )

            # 4. Paso 4: Construir el mapa de índices
            item_index: Dict[str, str] = {}
            for cat_id in catalog_ids:
                cat_res = await client.get(
                    f"/api/v2/catalogs/{cat_id}", headers=headers
                )
                if cat_res.status_code == 200:
                    cat_json = cat_res.json()
                    for item in _flatten_items(cat_json.get("items", [])):
                        val = item.get("value")
                        item_id = item.get("catalog_item_id") or item.get("id")
                        if val and item_id:
                            item_index[val] = item_id

            # 5. Guardar el estado sobrescribiendo el archivo anterior
            state = {
                "observatory_id": resolved_obs_id,
                "catalog_ids": {
                    cat.get("name", f"cat_{i}"): cid
                    for i, (cat, cid) in enumerate(
                        zip(bulk_payload, catalog_ids)
                    )
                },
                "item_index": item_index,
            }

            if STATE_FILE.exists():
                try:
                    with open(STATE_FILE, encoding="utf-8") as sf:
                        old_state = json.load(sf)
                        if isinstance(old_state, dict):
                            old_state.update(state)
                            state = old_state
                except Exception:
                    pass

            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)

            return json.dumps(
                {
                    "status": "success",
                    "observatory_id": resolved_obs_id,
                    "catalogs_registered": len(catalog_ids),
                    "indexed_items": len(item_index),
                    "catalogs_file_used": target_catalogs_path.name,
                    "state_file": str(STATE_FILE.resolve()),
                },
                ensure_ascii=False,
                indent=2,
            )