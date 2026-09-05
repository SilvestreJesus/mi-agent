import json
import os
from pathlib import Path
from typing import Any, Dict, List

import httpx
from fastmcp import FastMCP

JUB_URL = os.environ.get("JUB_API_URL", "http://localhost:5000")
JUB_USER = os.environ.get("JUB_USERNAME", "invitado")
JUB_PASS = os.environ.get("JUB_PASSWORD", "invitado")

CATALOGS_FILE = Path("data/catalogs.json")
STATE_FILE = Path(".state.json")

CATALOG_LEVELS = {
    "SPATIAL": 0,
    "TEMPORAL": 1,
    "INTEREST": 2,
    "REFERENCE": 3,
    "OBSERVABLE": 4,
}


def _is_conflict(status_code: int, detail: str) -> bool:
    if status_code in (403, 409):
        return True
    return any(
        k in detail.lower()
        for k in ("409", "403", "already", "duplicate", "exists", "forbidden")
    )


def _flatten_items(items: List[Dict[str, Any]]):
    for item in items:
        yield item
        if "children" in item and item["children"]:
            yield from _flatten_items(item["children"])


def register(mcp: FastMCP):
    """Registra las herramientas del Observatorio en el servidor FastMCP."""

    @mcp.tool(name="indexar_observatorio")
    async def indexar_observatorio(
        observatory_id: str,
        title: str,
        description: str
    ) -> str:
        """
        Registra e indiza un nuevo Observatorio en la API de JUB y procesa
        sus catálogos asociados guardando el estado.
        """
        if not CATALOGS_FILE.exists():
            return f"Error: No se encontró el archivo de catálogos en {CATALOGS_FILE}."

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=30.0) as client:
            headers = {}
            
            # 1. Autenticación usando la ruta correcta de la API: /api/v2/users/auth
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

            # 2. Crear Observatorio usando /api/v2/observatories
            obs_payload = {
                "observatory_id": observatory_id,
                "title": title,
                "description": description,
            }
            obs_res = await client.post("/api/v2/observatories", json=obs_payload, headers=headers)
            if obs_res.status_code not in (200, 201) and not _is_conflict(obs_res.status_code, obs_res.text):
                return f"Error al crear el Observatorio ({obs_res.status_code}): {obs_res.text}"

            # 3. Cargar e Ingestar Catálogos Bulk usando /api/v2/catalogs/bulk
            with open(CATALOGS_FILE, encoding="utf-8") as f:
                catalogs_data = json.load(f)

            bulk_res = await client.post("/api/v2/catalogs/bulk", json=catalogs_data, headers=headers)
            if bulk_res.status_code not in (200, 201):
                return f"Error en la ingesta bulk de catálogos ({bulk_res.status_code}): {bulk_res.text}"

            # Manejar la respuesta del bulk (puede ser lista directa o un objeto contenedor)
            bulk_json = bulk_res.json()
            if isinstance(bulk_json, dict):
                catalog_ids = bulk_json.get("catalog_ids", [])
            elif isinstance(bulk_json, list):
                catalog_ids = [c.get("catalog_id") or c.get("id") for c in bulk_json if isinstance(c, dict)]
            else:
                catalog_ids = []

            # 4. Enlazar Catálogos por nivel STORI usando /api/v2/observatories/{observatory_id}/catalogs
            for cat_id, cat_dict in zip(catalog_ids, catalogs_data):
                cat_type = cat_dict.get("catalog_type", "INTEREST")
                level = CATALOG_LEVELS.get(cat_type, 2)
                
                link_res = await client.post(
                    f"/api/v2/observatories/{observatory_id}/catalogs",
                    json={"catalog_id": cat_id, "level": level},
                    headers=headers,
                )
                if link_res.status_code not in (200, 201) and not _is_conflict(link_res.status_code, link_res.text):
                    print(f"Advertencia al enlazar catálogo {cat_id}: {link_res.text}")

            # 5. Mapear e Indizar Ítems usando /api/v2/catalogs/{cat_id}
            item_index: Dict[str, str] = {}
            for cat_id in catalog_ids:
                cat_res = await client.get(f"/api/v2/catalogs/{cat_id}", headers=headers)
                if cat_res.status_code == 200:
                    cat_json = cat_res.json()
                    for item in _flatten_items(cat_json.get("items", [])):
                        val = item.get("value")
                        item_id = item.get("catalog_item_id") or item.get("id")
                        if val and item_id:
                            item_index[val] = item_id

            # 6. Persistir el estado localmente
            state = {
                "observatory_id": observatory_id,
                "catalog_ids": {cat.get("name", f"cat_{i}"): cid for i, (cat, cid) in enumerate(zip(catalogs_data, catalog_ids))},
                "item_index": item_index,
            }
            STATE_FILE.parent.mkdir(exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)

            return json.dumps({
                "status": "success",
                "observatory_id": observatory_id,
                "catalogs_registered": len(catalog_ids),
                "indexed_items": len(item_index),
                "state_file": str(STATE_FILE.resolve())
            }, ensure_ascii=False)