import json
import os
from pathlib import Path
from typing import Any, Dict, List

import httpx
from fastmcp import FastMCP

JUB_URL = os.environ.get("JUB_API_URL", "http://localhost:5000")
JUB_USER = os.environ.get("JUB_USERNAME", "invitado")
JUB_PASS = os.environ.get("JUB_PASSWORD", "invitado")
STATE_FILE = Path(".state.json")


def _is_conflict(status_code: int, detail: str) -> bool:
    if status_code in (403, 409):
        return True
    return any(
        k in detail.lower()
        for k in ("409", "403", "already", "duplicate", "exists", "forbidden")
    )


def register(mcp: FastMCP):
    """Registra la herramienta de creación múltiple de productos en el servidor FastMCP."""

    @mcp.tool(name="crear_productos_multples")
    async def crear_productos_multples() -> str:
        """
        Carga el observatory_id desde .state.json, crea el producto principal de dataset
        y los productos anuales segmentados (2004 a 2014) en JUB V2, actualizando el estado.
        """
        if not STATE_FILE.exists():
            return f"Error: No se encontró el archivo de estado ({STATE_FILE}). Ejecuta los pasos previos."

        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)

        observatory_id = state.get("observatory_id")
        if not observatory_id:
            return "Error: No se encontró 'observatory_id' en .state.json."

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            headers = {}

            # 1. Autenticación usando la ruta estándar de la API v2
            login_endpoints = ["/api/v2/users/auth", "/v2/auth/login", "/auth/login"]
            token = None

            for endpoint in login_endpoints:
                try:
                    auth_res = await client.post(
                        endpoint,
                        json={
                            "username": JUB_USER,
                            "password": JUB_PASS,
                            "scope": "jub",
                            "expiration": "1h",
                            "renew_token": False
                        }
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

            # 2. Crear Product de entrada (Dataset Completo)
            entrada_payload = {
                "product_id": "prod-benceno-retc-dataset",
                "name": "Dataset Benceno RETC 2004-2014",
                "description": "Dataset completo de emisiones de benceno reportadas al RETC para México, 2004 a 2014.",
                "observatory_id": observatory_id,
                "catalog_item_ids": []
            }

            res = await client.post("/api/v2/products", json=entrada_payload, headers=headers)
            entrada_id = None

            if res.status_code in (200, 201):
                entrada_id = res.json().get("product_id")
            elif _is_conflict(res.status_code, res.text) or res.status_code == 400:
                entrada_id = entrada_payload["product_id"]

            if not entrada_id:
                entrada_id = entrada_payload["product_id"]

            # 3. Crear Products por año (2004 - 2014)
            years = list(range(2004, 2015))
            product_ids = {}

            for year in years:
                prod_id = f"prod-benceno-retc-{year}"
                year_payload = {
                    "product_id": prod_id,
                    "name": f"Benceno RETC — {year}",
                    "description": f"Emisiones de benceno reportadas al RETC en México durante {year}.",
                    "observatory_id": observatory_id,
                    "catalog_item_ids": []
                }

                r = await client.post("/api/v2/products", json=year_payload, headers=headers)
                if r.status_code in (200, 201, 400, 409, 403):
                    product_ids[year] = prod_id

            # 4. Actualizar archivo de estado local
            state["product_ids"] = {str(k): v for k, v in product_ids.items()}
            state["entrada_product_id"] = entrada_id

            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)

            return json.dumps({
                "status": "success",
                "observatory_id": observatory_id,
                "entrada_product_id": entrada_id,
                "products_by_year_count": len(product_ids),
                "state_file": str(STATE_FILE.resolve())
            }, ensure_ascii=False)