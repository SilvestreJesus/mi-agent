import json
from typing import Optional, Dict, Any
import httpx
from fastmcp import FastMCP

from config import JUB_URL, JUB_USER, JUB_PASS, STATE_FILE


async def _get_auth_token(client: httpx.AsyncClient) -> Optional[str]:
    """Obtiene el token Bearer autenticando contra la API v2 de JUB."""
    try:
        res = await client.post(
            "/api/v2/users/auth",
            json={"username": JUB_USER, "password": JUB_PASS}
        )
        if res.status_code in (200, 201):
            data = res.json()
            return data.get("access_token") or data.get("token") or data.get("accessToken")
    except Exception as e:
        print(f"Advertencia de autenticación: {e}")
    return None


def register(mcp: FastMCP):
    """Registra el conjunto de herramientas de consulta en el servidor FastMCP."""

    @mcp.tool(name="listar_recursos_generales")
    async def listar_recursos_generales() -> str:
        """
        Lista observatorios, catálogos, datasources y productos disponibles en JUB v2.
        """
        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            token = await _get_auth_token(client)
            headers = {"Authorization": f"Bearer {token}"} if token else {}

            results = {}

            # Observatorios
            res_obs = await client.get("/api/v2/observatories", headers=headers)
            results["observatories"] = res_obs.json() if res_obs.status_code == 200 else []

            # Catálogos
            res_cats = await client.get("/api/v2/catalogs", headers=headers)
            results["catalogs"] = res_cats.json() if res_cats.status_code == 200 else []

            # DataSources
            res_srcs = await client.get("/api/v2/datasources", headers=headers)
            results["datasources"] = res_srcs.json() if res_srcs.status_code == 200 else []

            # Productos
            res_prods = await client.get("/api/v2/products", headers=headers)
            results["products"] = res_prods.json() if res_prods.status_code == 200 else []

            return json.dumps(results, ensure_ascii=False, indent=2)


    @mcp.tool(name="obtener_detalle_recurso")
    async def obtener_detalle_recurso(
        observatory_id: Optional[str] = None,
        source_id: Optional[str] = None
    ) -> str:
        """
        Obtiene la información detallada de un Observatorio y/o DataSource por su ID.
        Si no se especifican IDs explícitamente, se intentan leer desde el archivo .state.json.
        """
        target_obs_id = observatory_id
        target_src_id = source_id

        # Fallback al archivo de estado si no se proveen IDs
        if (not target_obs_id or not target_src_id) and STATE_FILE.exists():
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
                target_obs_id = target_obs_id or state.get("observatory_id")
                target_src_id = target_src_id or state.get("source_id")

        if not target_obs_id and not target_src_id:
            return "Error: No se proporcionaron IDs válidos y no hay contexto en .state.json."

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            token = await _get_auth_token(client)
            headers = {"Authorization": f"Bearer {token}"} if token else {}

            details = {}

            if target_obs_id:
                res = await client.get(f"/api/v2/observatories/{target_obs_id}", headers=headers)
                details["observatory"] = res.json() if res.status_code == 200 else f"Error: {res.status_code}"

            if target_src_id:
                res = await client.get(f"/api/v2/datasources/{target_src_id}", headers=headers)
                details["datasource"] = res.json() if res.status_code == 200 else f"Error: {res.status_code}"

            return json.dumps(details, ensure_ascii=False, indent=2)


    @mcp.tool(name="consultar_records_dsl")
    async def consultar_records_dsl(
        query: str,  # Sin valor por defecto. El usuario/agente DEBE especificarlo.
        limit: int = 10,
        skip: int = 0
    ) -> str:
        """
        Ejecuta consultas DSL sobre DataRecords (ej: jub.v1.VS(MX_01).VT(Y2024).VI(METRICA)).
        Usa por defecto el source_id almacenado en .state.json si existe.
        """
        if not query:
            return "Error: Debes proporcionar una cadena de consulta DSL válida en el parámetro 'query'."

        if not STATE_FILE.exists():
            return f"Error: No se encontró el archivo de estado ({STATE_FILE}) para deducir el source_id."

        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)

        source_id = state.get("source_id")
        if not source_id:
            return "Error: No hay 'source_id' en .state.json para ejecutar la consulta."

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            token = await _get_auth_token(client)
            headers = {"Authorization": f"Bearer {token}"} if token else {}


            payload = {
                "query": query,
                "limit": limit,
                "skip": skip
            }

            res = await client.post(f"/api/v2/datasources/{source_id}/query", json=payload, headers=headers)

            if res.status_code == 200:
                results = res.json()
                return json.dumps({
                    "status": "success",
                    "query": query,
                    "source_id": source_id,
                    "total_count": len(results) if isinstance(results, list) else 0,
                    "results": results
                }, ensure_ascii=False, indent=2)
            else:
                return f"Error en la consulta DSL ({res.status_code}): {res.text}"


    @mcp.tool(name="listar_productos_observatorio")
    async def listar_productos_observatorio(observatory_id: Optional[str] = None) -> str:
        """
        Lista los productos vinculados a un Observatorio específico.
        """
        if not observatory_id and STATE_FILE.exists():
            with open(STATE_FILE, encoding="utf-8") as f:
                observatory_id = json.load(f).get("observatory_id")

        if not observatory_id:
            return "Error: Se requiere un 'observatory_id' o haber indexado previamente (.state.json)."

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            token = await _get_auth_token(client)
            headers = {"Authorization": f"Bearer {token}"} if token else {}

            res = await client.get(f"/api/v2/observatories/{observatory_id}/products", headers=headers)

            if res.status_code == 200:
                return json.dumps({
                    "observatory_id": observatory_id,
                    "products": res.json()
                }, ensure_ascii=False, indent=2)
            return f"Error al obtener productos ({res.status_code}): {res.text}"


    @mcp.tool(name="listar_catalogos_observatorio")
    async def listar_catalogos_observatorio(observatory_id: Optional[str] = None) -> str:
        """
        Lista los catálogos enlazados a un Observatorio específico.
        """
        if not observatory_id and STATE_FILE.exists():
            with open(STATE_FILE, encoding="utf-8") as f:
                observatory_id = json.load(f).get("observatory_id")

        if not observatory_id:
            return "Error: Se requiere un 'observatory_id' o haber indexado previamente (.state.json)."

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            token = await _get_auth_token(client)
            headers = {"Authorization": f"Bearer {token}"} if token else {}

            res = await client.get(f"/api/v2/observatories/{observatory_id}/catalogs", headers=headers)

            if res.status_code == 200:
                return json.dumps({
                    "observatory_id": observatory_id,
                    "catalogs": res.json()
                }, ensure_ascii=False, indent=2)
            return f"Error al obtener catálogos ({res.status_code}): {res.text}"