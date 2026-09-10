import json
import re
from typing import Dict, Optional
import httpx
from fastmcp import FastMCP

from config import JUB_URL, JUB_USER, JUB_PASS, STATE_FILE


def _is_conflict(status_code: int, detail: str) -> bool:
    if status_code in (403, 409):
        return True
    return any(
        k in detail.lower()
        for k in ("409", "403", "already", "duplicate", "exists", "forbidden")
    )

def _generate_slug(text: str) -> str:
    """Convierte texto normal en un ID válido (ej. 'Benceno RETC' -> 'benceno-retc')"""
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def register(mcp: FastMCP):
    """Registra la herramienta de creación múltiple de productos en FastMCP."""

    @mcp.tool(name="crear_productos_multiples")
    async def crear_productos_multiples(
        product_name_base: str,
        product_desc_base: str,
        start_year: int = 2004,
        end_year: int = 2014
    ) -> str:
        """
        Carga el observatory_id desde .state.json, crea el producto principal de dataset
        y los productos anuales segmentados en JUB V2, los vincula al observatorio
        y actualiza el archivo de estado.
        """
        if not product_name_base or not product_desc_base:
            return json.dumps({
                "status": "error",
                "message": "Faltan parámetros requeridos: 'product_name_base' y 'product_desc_base'."
            }, ensure_ascii=False)

        if not STATE_FILE.exists():
            return f"Error: No se encontró el archivo de estado en {STATE_FILE}. Ejecuta los pasos previos."

        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)

        observatory_id = state.get("observatory_id")
        if not observatory_id:
            return "Error: No se encontró 'observatory_id' en .state.json."

        base_slug = _generate_slug(product_name_base)

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            headers = {}

            # 1. Autenticación
            try:
                auth_res = await client.post(
                    "/api/v2/users/auth",
                    json={"username": JUB_USER, "password": JUB_PASS}
                )
                if auth_res.status_code in (200, 201):
                    data = auth_res.json()
                    token = data.get("access_token") or data.get("token") or data.get("accessToken")
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
            except Exception as e:
                print(f"Advertencia de autenticación: {e}")

            # 2. Paso 1 — Creando Product de entrada 
            entrada_id = f"prod-{base_slug}-dataset"
            entrada_payload = {
                "product_id": entrada_id,
                "name": f"Dataset {product_name_base} {start_year}-{end_year}",
                "description": f"Dataset completo: {product_desc_base} para {start_year} a {end_year}.",
                "observatory_id": observatory_id,
                "catalog_item_ids": []
            }

            res = await client.post("/api/v2/products", json=entrada_payload, headers=headers)
            if res.status_code not in (200, 201) and not _is_conflict(res.status_code, res.text):
                print(f"Nota: El producto principal respondió {res.status_code}")

            # 3. Paso 2 — Creando Products individuales por año
            years = list(range(start_year, end_year + 1))
            product_ids_by_year: Dict[str, str] = {}

            for year in years:
                prod_id = f"prod-{base_slug}-{year}"
                year_payload = {
                    "product_id": prod_id,
                    "name": f"{product_name_base} — {year}",
                    "description": f"{product_desc_base} durante {year}.",
                    "observatory_id": observatory_id,
                    "catalog_item_ids": []
                }

                r = await client.post("/api/v2/products", json=year_payload, headers=headers)
                if r.status_code in (200, 201) or _is_conflict(r.status_code, r.text):
                    product_ids_by_year[str(year)] = prod_id
                else:
                    print(f"Error creando producto para {year}: {r.text}")

            # 4. Actualizar el archivo .state.json idéntico al script
            state["entrada_product_id"] = entrada_id
            state["product_ids"] = product_ids_by_year

            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)

            return json.dumps({
                "status": "success",
                "observatory_id": observatory_id,
                "entrada_product_id": entrada_id,
                "products_by_year_count": len(product_ids_by_year),
                "state_file": str(STATE_FILE.resolve())
            }, ensure_ascii=False, indent=2)