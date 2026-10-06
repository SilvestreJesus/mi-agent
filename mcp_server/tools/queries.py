import json
import unicodedata
from typing import Optional, Dict, Any, List

import httpx
from fastmcp import FastMCP

from config import JUB_URL, JUB_USER, JUB_PASS, STATE_FILE


# ============================================================
# UTILIDADES
# ============================================================

async def _get_auth_token(client: httpx.AsyncClient) -> Optional[str]:
    """Obtiene el token Bearer autenticando contra JUB API v2."""
    try:
        res = await client.post(
            "/api/v2/users/auth",
            json={"username": JUB_USER, "password": JUB_PASS}
        )

        if res.status_code in (200, 201):
            data = res.json()
            return data.get("access_token") or data.get("token") or data.get("accessToken")

        print(f"[JUB AUTH] Error {res.status_code}: {res.text}")

    except Exception as e:
        print(f"[JUB AUTH] Error de conexión: {e}")

    return None


async def _get_headers(client: httpx.AsyncClient) -> Dict[str, str]:
    token = await _get_auth_token(client)
    return {"Authorization": f"Bearer {token}"} if token else {}


def _normalizar(texto: Any) -> str:
    """Normaliza texto para búsquedas por nombre ignorando mayúsculas y acentos."""
    if texto is None:
        return ""

    texto = str(texto).strip().lower()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")

    return " ".join(texto.split())


def _extraer_lista(data: Any, claves: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Convierte diferentes formatos de respuesta de JUB en una lista."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if not isinstance(data, dict):
        return []

    claves = (claves or []) + ["items", "results"]

    for clave in claves:
        valor = data.get(clave)

        if isinstance(valor, list):
            return [item for item in valor if isinstance(item, dict)]

    if isinstance(data.get("data"), list):
        return [item for item in data["data"] if isinstance(item, dict)]

    if isinstance(data.get("data"), dict):
        for clave in claves:
            valor = data["data"].get(clave)

            if isinstance(valor, list):
                return [item for item in valor if isinstance(item, dict)]

    return []


def _observatory_id(obs: Dict[str, Any]) -> Optional[str]:
    return obs.get("observatory_id") or obs.get("id") or obs.get("_id") or obs.get("obid")


def _observatory_name(obs: Dict[str, Any]) -> str:
    return obs.get("title") or obs.get("name") or obs.get("observatory_name") or "Sin título"


def _product_id(product: Dict[str, Any]) -> Optional[str]:
    return product.get("product_id") or product.get("id") or product.get("_id") or product.get("pid")


def _product_name(product: Dict[str, Any]) -> str:
    return product.get("name") or product.get("title") or product.get("product_name") or "Sin nombre"


def _product_observatory_id(product: Dict[str, Any]) -> Optional[str]:
    for key in ("observatory_id", "observatoryId", "obid"):
        value = product.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    observatory = product.get("observatory")

    if isinstance(observatory, str) and observatory.strip():
        return observatory.strip()

    if isinstance(observatory, dict):
        return _observatory_id(observatory)

    metadata = product.get("metadata")

    if isinstance(metadata, dict):
        for key in ("observatory_id", "observatoryId", "obid"):
            value = metadata.get(key)

            if isinstance(value, str) and value.strip():
                return value.strip()

    return None


async def _get_json(client: httpx.AsyncClient, endpoint: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """Ejecuta un GET y devuelve una estructura uniforme."""
    try:
        res = await client.get(endpoint, headers=headers)

        if res.status_code != 200:
            return {
                "ok": False,
                "status_code": res.status_code,
                "endpoint": endpoint,
                "error": res.text
            }

        return {
            "ok": True,
            "status_code": res.status_code,
            "endpoint": endpoint,
            "data": res.json()
        }

    except Exception as e:
        return {
            "ok": False,
            "endpoint": endpoint,
            "error": str(e)
        }


async def _obtener_observatorios() -> Dict[str, Any]:
    """Consulta internamente todos los observatorios reales de JUB."""
    async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
        headers = await _get_headers(client)
        result = await _get_json(client, "/api/v2/observatories", headers)

    if not result.get("ok"):
        return result

    observatorios = _extraer_lista(
        result.get("data"),
        ["observatories", "observatorios"]
    )

    normalized = []

    for obs in observatorios:
        normalized.append({
            "observatory_id": _observatory_id(obs),
            "title": _observatory_name(obs),
            "status": obs.get("status"),
            "visible": obs.get("visible"),
            "description": obs.get("description"),
            "raw": obs
        })

    return {
        "ok": True,
        "total": len(normalized),
        "observatories": normalized
    }


async def _buscar_observatorio(consulta: str) -> Dict[str, Any]:
    """Busca internamente un observatorio por ID o nombre."""
    resultado = await _obtener_observatorios()

    if not resultado.get("ok"):
        return resultado

    observatorios = resultado.get("observatories", [])
    consulta_original = consulta.strip()
    consulta_normalizada = _normalizar(consulta)

    # Coincidencia exacta por ID.
    for obs in observatorios:
        if str(obs.get("observatory_id") or "") == consulta_original:
            return {
                "ok": True,
                "found": True,
                "match_type": "id",
                "observatory": obs
            }

    # Coincidencia exacta por nombre.
    for obs in observatorios:
        if _normalizar(obs.get("title")) == consulta_normalizada:
            return {
                "ok": True,
                "found": True,
                "match_type": "exact_name",
                "observatory": obs
            }

    # Coincidencia parcial.
    parciales = []

    for obs in observatorios:
        nombre = _normalizar(obs.get("title"))

        if consulta_normalizada in nombre or nombre in consulta_normalizada:
            parciales.append(obs)

    if len(parciales) == 1:
        return {
            "ok": True,
            "found": True,
            "match_type": "partial_name",
            "observatory": parciales[0]
        }

    if len(parciales) > 1:
        return {
            "ok": True,
            "found": False,
            "ambiguous": True,
            "total": len(parciales),
            "matches": parciales,
            "message": "Se encontraron varios observatorios que coinciden con el nombre."
        }

    # Búsqueda por palabras.
    palabras = [p for p in consulta_normalizada.split() if len(p) >= 3]
    puntuados = []

    for obs in observatorios:
        nombre = _normalizar(obs.get("title"))
        puntos = sum(1 for palabra in palabras if palabra in nombre)

        if puntos > 0:
            puntuados.append((puntos, obs))

    if puntuados:
        puntuados.sort(key=lambda item: item[0], reverse=True)
        mejor_puntaje = puntuados[0][0]
        mejores = [obs for puntos, obs in puntuados if puntos == mejor_puntaje]

        if len(mejores) == 1:
            return {
                "ok": True,
                "found": True,
                "match_type": "keywords",
                "observatory": mejores[0]
            }

        return {
            "ok": True,
            "found": False,
            "ambiguous": True,
            "total": len(mejores),
            "matches": mejores,
            "message": "La búsqueda coincide con varios observatorios."
        }

    return {
        "ok": True,
        "found": False,
        "message": f"No se encontró un observatorio que coincida con '{consulta}'."
    }


async def _productos_observatorio(observatory_id: str) -> Dict[str, Any]:
    """Consulta los productos pertenecientes a un observatorio."""
    async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
        headers = await _get_headers(client)

        endpoint = f"/api/v2/observatories/{observatory_id}/products"
        result = await _get_json(client, endpoint, headers)

    if not result.get("ok"):
        return result

    products = _extraer_lista(result.get("data"), ["products", "productos"])
    normalized = []

    for product in products:
        normalized.append({
            "product_id": _product_id(product),
            "name": _product_name(product),
            "description": product.get("description"),
            "status": product.get("status"),
            "observatory_id": _product_observatory_id(product) or observatory_id,
            "raw": product
        })

    return {
        "ok": True,
        "observatory_id": observatory_id,
        "total": len(normalized),
        "products": normalized
    }


# ============================================================
# HERRAMIENTAS MCP
# ============================================================

def register(mcp: FastMCP):
    """Registra herramientas específicas de consulta para el Agente JUB."""

    # ========================================================
    # CONEXIÓN
    # ========================================================

    @mcp.tool(name="verificar_conexion_jub")
    async def verificar_conexion_jub() -> str:
        """
        Verifica la conexión real con la API de JUB.
        No requiere parámetros.
        """
        try:
            async with httpx.AsyncClient(base_url=JUB_URL, timeout=15.0) as client:
                token = await _get_auth_token(client)

            result = {
                "ok": token is not None,
                "connected": token is not None,
                "jub_url": JUB_URL
            }

            if not token:
                result["message"] = "No fue posible autenticar con la API de JUB."
            else:
                result["message"] = "La conexión con JUB está activa."

            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({
                "ok": False,
                "connected": False,
                "jub_url": JUB_URL,
                "error": str(e)
            }, ensure_ascii=False, indent=2)


    # ========================================================
    # OBSERVATORIOS
    # ========================================================

    @mcp.tool(name="listar_observatorios")
    async def listar_observatorios() -> str:
        """
        Lista TODOS los observatorios registrados actualmente en JUB.

        No recibe parámetros.

        Utilizar para:
        - ¿Qué observatorios existen?
        - Lista los observatorios.
        - ¿Cuántos observatorios hay?
        - ¿Qué observatorios tengo indexados?
        """
        result = await _obtener_observatorios()
        return json.dumps(result, ensure_ascii=False, indent=2)


    @mcp.tool(name="buscar_observatorio")
    async def buscar_observatorio(consulta: str) -> str:
        """
        Busca UN observatorio por su nombre o ID.

        Parámetro:
        - consulta: nombre o ID real que escribió el usuario.

        Utilizar para:
        - Dame el ID del observatorio hola.
        - Busca Emisiones de Benceno RETC México.
        - ¿Existe el observatorio X?

        No inventar nombres ni IDs.
        """
        if not consulta or not consulta.strip():
            return json.dumps({
                "ok": False,
                "error": "Debes proporcionar el nombre o ID del observatorio."
            }, ensure_ascii=False, indent=2)

        result = await _buscar_observatorio(consulta)
        return json.dumps(result, ensure_ascii=False, indent=2)


    @mcp.tool(name="obtener_observatorio")
    async def obtener_observatorio(observatory_id: str) -> str:
        """
        Obtiene la información detallada de UN observatorio mediante su ID real.
        """
        if not observatory_id or not observatory_id.strip():
            return json.dumps({
                "ok": False,
                "error": "Se requiere observatory_id."
            }, ensure_ascii=False, indent=2)

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            headers = await _get_headers(client)
            result = await _get_json(
                client,
                f"/api/v2/observatories/{observatory_id.strip()}",
                headers
            )

        if not result.get("ok"):
            # Fallback al listado en caso de que JUB no exponga detalle directo.
            result = await _buscar_observatorio(observatory_id)

        return json.dumps(result, ensure_ascii=False, indent=2)


    # ========================================================
    # PRODUCTOS
    # ========================================================

    @mcp.tool(name="listar_todos_productos")
    async def listar_todos_productos() -> str:
        """
        Lista TODOS los productos registrados en JUB y los relaciona con su observatorio.

        No recibe parámetros.

        Utilizar para:
        - ¿Qué productos hay?
        - Lista todos los productos.
        - ¿Cuántos productos existen?
        - Muéstrame todos los productos con su observatory_id.

        IMPORTANTE:
        No requiere observatory_id.
        Nunca inventar un observatory_id para ejecutar esta herramienta.
        """
        observatorios_result = await _obtener_observatorios()

        if not observatorios_result.get("ok"):
            return json.dumps(observatorios_result, ensure_ascii=False, indent=2)

        observatorios = observatorios_result.get("observatories", [])
        productos_totales = []
        errores = []

        for obs in observatorios:
            observatory_id = obs.get("observatory_id")

            if not observatory_id:
                continue

            result = await _productos_observatorio(observatory_id)

            if not result.get("ok"):
                errores.append({
                    "observatory_id": observatory_id,
                    "observatory_title": obs.get("title"),
                    "error": result.get("error"),
                    "status_code": result.get("status_code")
                })
                continue

            for product in result.get("products", []):
                productos_totales.append({
                    "product_id": product.get("product_id"),
                    "name": product.get("name"),
                    "description": product.get("description"),
                    "status": product.get("status"),
                    "observatory_id": observatory_id,
                    "observatory_title": obs.get("title")
                })

        return json.dumps({
            "ok": True,
            "total_observatories": len(observatorios),
            "total_products": len(productos_totales),
            "products": productos_totales,
            "errors": errores
        }, ensure_ascii=False, indent=2)


    @mcp.tool(name="listar_productos_observatorio")
    async def listar_productos_observatorio(observatory_id: str) -> str:
        """
        Lista los productos de UN observatorio específico.

        Requiere obligatoriamente observatory_id.

        No utiliza .state.json como fallback porque una consulta
        conversacional debe utilizar exactamente el observatorio solicitado.
        """
        if not observatory_id or not observatory_id.strip():
            return json.dumps({
                "ok": False,
                "error": "Se requiere observatory_id. No se utilizará un ID de ejemplo ni .state.json."
            }, ensure_ascii=False, indent=2)

        result = await _productos_observatorio(observatory_id.strip())

        if result.get("ok"):
            obs = await _buscar_observatorio(observatory_id.strip())

            if obs.get("found"):
                title = obs.get("observatory", {}).get("title")

                for product in result.get("products", []):
                    product["observatory_title"] = title

        return json.dumps(result, ensure_ascii=False, indent=2)


    @mcp.tool(name="buscar_producto")
    async def buscar_producto(consulta: str) -> str:
        """
        Busca un producto por nombre o ID en todos los observatorios.
        Devuelve también el observatory_id al que pertenece.
        """
        if not consulta or not consulta.strip():
            return json.dumps({
                "ok": False,
                "error": "Debes proporcionar el nombre o ID del producto."
            }, ensure_ascii=False, indent=2)

        observatorios_result = await _obtener_observatorios()

        if not observatorios_result.get("ok"):
            return json.dumps(observatorios_result, ensure_ascii=False, indent=2)

        consulta_original = consulta.strip()
        consulta_normalizada = _normalizar(consulta)
        coincidencias = []

        for obs in observatorios_result.get("observatories", []):
            observatory_id = obs.get("observatory_id")

            if not observatory_id:
                continue

            result = await _productos_observatorio(observatory_id)

            if not result.get("ok"):
                continue

            for product in result.get("products", []):
                product_id = str(product.get("product_id") or "")
                nombre = _normalizar(product.get("name"))

                if product_id == consulta_original or nombre == consulta_normalizada or consulta_normalizada in nombre:
                    coincidencias.append({
                        "product_id": product.get("product_id"),
                        "name": product.get("name"),
                        "description": product.get("description"),
                        "status": product.get("status"),
                        "observatory_id": observatory_id,
                        "observatory_title": obs.get("title")
                    })

        return json.dumps({
            "ok": True,
            "found": bool(coincidencias),
            "total": len(coincidencias),
            "products": coincidencias
        }, ensure_ascii=False, indent=2)


    # ========================================================
    # CATÁLOGOS
    # ========================================================

    @mcp.tool(name="listar_catalogos_observatorio")
    async def listar_catalogos_observatorio(observatory_id: str) -> str:
        """
        Lista los catálogos asociados a UN observatorio específico.

        Requiere obligatoriamente observatory_id.
        """
        if not observatory_id or not observatory_id.strip():
            return json.dumps({
                "ok": False,
                "error": "Se requiere observatory_id."
            }, ensure_ascii=False, indent=2)

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            headers = await _get_headers(client)
            result = await _get_json(
                client,
                f"/api/v2/observatories/{observatory_id.strip()}/catalogs",
                headers
            )

        if not result.get("ok"):
            return json.dumps(result, ensure_ascii=False, indent=2)

        catalogs = _extraer_lista(
            result.get("data"),
            ["catalogs", "catalogues", "catalogos"]
        )

        return json.dumps({
            "ok": True,
            "observatory_id": observatory_id.strip(),
            "total": len(catalogs),
            "catalogs": catalogs
        }, ensure_ascii=False, indent=2)


    # ========================================================
    # DATASOURCES
    # ========================================================

    @mcp.tool(name="listar_datasources")
    async def listar_datasources() -> str:
        """
        Lista todos los DataSources registrados actualmente en JUB.
        No recibe parámetros.
        """
        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            headers = await _get_headers(client)
            result = await _get_json(client, "/api/v2/datasources", headers)

        if not result.get("ok"):
            return json.dumps(result, ensure_ascii=False, indent=2)

        datasources = _extraer_lista(result.get("data"), ["datasources", "sources"])

        return json.dumps({
            "ok": True,
            "total": len(datasources),
            "datasources": datasources
        }, ensure_ascii=False, indent=2)


    @mcp.tool(name="obtener_datasource")
    async def obtener_datasource(source_id: str) -> str:
        """
        Obtiene un DataSource específico mediante source_id.
        """
        if not source_id or not source_id.strip():
            return json.dumps({
                "ok": False,
                "error": "Se requiere source_id."
            }, ensure_ascii=False, indent=2)

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            headers = await _get_headers(client)
            result = await _get_json(
                client,
                f"/api/v2/datasources/{source_id.strip()}",
                headers
            )

        return json.dumps(result, ensure_ascii=False, indent=2)


    # ========================================================
    # CONSULTAS DSL
    # ========================================================

    @mcp.tool(name="consultar_records_dsl")
    async def consultar_records_dsl(query: str, limit: int = 10, skip: int = 0) -> str:
        """
        Ejecuta una consulta DSL sobre los DataRecords.

        Esta herramienta conserva el uso de .state.json porque las consultas
        DSL necesitan conocer el source_id generado durante la indexación.
        """
        if not query or not query.strip():
            return json.dumps({
                "ok": False,
                "error": "Debes proporcionar una consulta DSL válida."
            }, ensure_ascii=False, indent=2)

        if not STATE_FILE.exists():
            return json.dumps({
                "ok": False,
                "error": f"No se encontró el archivo de estado {STATE_FILE}."
            }, ensure_ascii=False, indent=2)

        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            return json.dumps({
                "ok": False,
                "error": f"No se pudo leer .state.json: {e}"
            }, ensure_ascii=False, indent=2)

        source_id = state.get("source_id")

        if not source_id:
            return json.dumps({
                "ok": False,
                "error": "No existe source_id en .state.json."
            }, ensure_ascii=False, indent=2)

        payload = {
            "query": query.strip(),
            "limit": max(1, min(limit, 1000)),
            "skip": max(0, skip)
        }

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            headers = await _get_headers(client)

            try:
                res = await client.post(
                    f"/api/v2/datasources/{source_id}/query",
                    json=payload,
                    headers=headers
                )
            except Exception as e:
                return json.dumps({
                    "ok": False,
                    "source_id": source_id,
                    "error": str(e)
                }, ensure_ascii=False, indent=2)

        if res.status_code != 200:
            return json.dumps({
                "ok": False,
                "source_id": source_id,
                "status_code": res.status_code,
                "error": res.text
            }, ensure_ascii=False, indent=2)

        results = res.json()

        return json.dumps({
            "ok": True,
            "query": query.strip(),
            "source_id": source_id,
            "total_count": len(results) if isinstance(results, list) else None,
            "results": results
        }, ensure_ascii=False, indent=2)


    # ========================================================
    # RESUMEN DE OBSERVATORIO
    # ========================================================

    @mcp.tool(name="resumen_observatorio")
    async def resumen_observatorio(observatory_id: str) -> str:
        """
        Recupera la información principal, productos y catálogos
        de un observatorio mediante su ID.
        """
        if not observatory_id or not observatory_id.strip():
            return json.dumps({
                "ok": False,
                "error": "Se requiere observatory_id."
            }, ensure_ascii=False, indent=2)

        observatory_id = observatory_id.strip()

        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            headers = await _get_headers(client)

            detalle = await _get_json(
                client,
                f"/api/v2/observatories/{observatory_id}",
                headers
            )

            catalogs_result = await _get_json(
                client,
                f"/api/v2/observatories/{observatory_id}/catalogs",
                headers
            )

        products_result = await _productos_observatorio(observatory_id)

        catalogs = []

        if catalogs_result.get("ok"):
            catalogs = _extraer_lista(
                catalogs_result.get("data"),
                ["catalogs", "catalogues", "catalogos"]
            )

        return json.dumps({
            "ok": True,
            "observatory_id": observatory_id,
            "observatory": detalle.get("data") if detalle.get("ok") else detalle,
            "products": products_result.get("products", []) if products_result.get("ok") else [],
            "catalogs": catalogs,
            "products_error": None if products_result.get("ok") else products_result,
            "catalogs_error": None if catalogs_result.get("ok") else catalogs_result
        }, ensure_ascii=False, indent=2)