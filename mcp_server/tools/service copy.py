import json
from typing import Optional
import httpx
from fastmcp import FastMCP


from config import JUB_URL, JUB_USER, JUB_PASS, STATE_FILE

_token: Optional[str] = None


async def get_auth_token() -> Optional[str]:
    """Obtiene o reutiliza el token Bearer usando el endpoint v2 oficial."""
    global _token
    if _token is None:
        async with httpx.AsyncClient(base_url=JUB_URL) as client:
            res = await client.post(
                "/api/v2/users/auth",
                json={"username": JUB_USER, "password": JUB_PASS}
            )
            if res.status_code in (200, 201):
                data = res.json()
                _token = data.get("access_token") or data.get("token") or data.get("accessToken")
    return _token


def register(mcp: FastMCP):
    """Registra la herramienta de consulta de servicios (Building Blocks / SVC) en FastMCP."""

    @mcp.tool(name="consultar_servicios_svc")
    async def consultar_servicios_svc(
        query: str, 
        limit: int = 10,
        skip: int = 0
    ) -> str:
        """
        Consulta los servicios vinculados al Observatorio activo o ejecuta 
        una consulta DSL en la API v2 de JUB (ej: jub.v1.SVC(name=mi-servicio)).
        """
        if not query:
            return "Error: Debes proporcionar una cadena de consulta DSL válida en el parámetro 'query'."

        if not STATE_FILE.exists():
            return f"Error: No se encontró el archivo de estado ({STATE_FILE}). Ejecuta los pasos previos."

        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)

        observatory_id = state.get("observatory_id")
        if not observatory_id:
            return "Error: No se encontró 'observatory_id' en .state.json."

        token = await get_auth_token()
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(base_url=JUB_URL, headers=headers, timeout=60.0) as client:
            try:
                # 1. Intento de búsqueda global por DSL vía /api/v2/search
                search_payload = {
                    "query": query,
                    "observatory_id": observatory_id,
                    "limit": limit,
                    "skip": skip
                }
                search_res = await client.post("/api/v2/search", json=search_payload)

                query_results = []
                if search_res.status_code == 200:
                    query_results = search_res.json()
                else:
                    # 2. Fallback: Obtener lista de servicios enlazados al observatorio /api/v2/observatories/{id}/services
                    svc_res = await client.get(f"/api/v2/observatories/{observatory_id}/services")
                    if svc_res.status_code == 200:
                        query_results = svc_res.json()
                    else:
                        return f"Error al consultar servicios ({svc_res.status_code}): {svc_res.text}"

                return json.dumps({
                    "status": "success",
                    "query_executed": query,
                    "observatory_id": observatory_id,
                    "total_results": len(query_results) if isinstance(query_results, list) else 1,
                    "results": query_results
                }, ensure_ascii=False, indent=2)

            except Exception as e:
                return f"Error inesperado al consultar los servicios vía HTTP: {str(e)}"