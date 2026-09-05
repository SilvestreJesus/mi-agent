import os
from jub.client.v2 import JubClientBuilder

_client = None


async def get_client():
    """Retorna la instancia global del cliente JUB (Paso 0).

    Lee las variables del archivo .env con fallback a localhost.
    """
    global _client
    if _client is None:
        api_url = os.environ.get("JUB_API_URL", "http://localhost:5000")
        username = os.environ.get("JUB_USERNAME", "invitado")
        password = os.environ.get("JUB_PASSWORD", "invitado")

        result = await JubClientBuilder(
            api_url=api_url,
            username=username,
            password=password,
        ).build()

        if result.is_err:
            raise RuntimeError(f"JUB auth failed: {result.unwrap_err()}")
        _client = result.unwrap()
    return _client