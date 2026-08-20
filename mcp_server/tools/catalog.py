"""Tools de consulta estilo "jub": inspiradas en jub-agent/mcp/tools/products.py
y jub-agent/mcp/tools/search.py, pero contra un catálogo mock en memoria
(`data/mock_catalog.json`) para que el tutorial no dependa de la API real
ni de credenciales.

Igual que en jub-agent, los docstrings son deliberadamente detallados:
son lo que el LLM lee para entender la mini-DSL de `search_catalogo` y
generar consultas válidas sin que el humano tenga que explicárselo en el
prompt del sistema.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "mock_catalog.json"
_OPS = {
    ">=": lambda v, x: v >= x,
    "<=": lambda v, x: v <= x,
    ">": lambda v, x: v > x,
    "<": lambda v, x: v < x,
    "=": lambda v, x: str(v) == str(x),
}
# Orden importa: probar los operadores de 2 caracteres antes que "=" solo.
_CLAUSE_RE = re.compile(r"(\w+)\s*(>=|<=|>|<|=)\s*(\S+)")


def _load() -> list[dict]:
    return json.loads(_DATA_PATH.read_text(encoding="utf-8"))


def _coerce(value: str):
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def _matches(item: dict, field: str, op: str, raw_value: str) -> bool:
    if field not in item:
        return False
    target = _coerce(raw_value)
    current = item[field]
    if isinstance(current, (int, float)) and not isinstance(target, str):
        return _OPS[op](current, target)
    return _OPS[op](current, raw_value)


def register(mcp) -> None:
    @mcp.tool()
    def list_catalogo(limit: int = 100) -> list[dict]:
        """Lista las estaciones/sensores del catálogo (hasta `limit` resultados).

        Cada elemento tiene: id, nombre, tipo (clima|aire|agua), region
        (Norte|Centro|Sur), valor (numérico) y unidad.
        """
        return _load()[:limit]

    @mcp.tool()
    def get_item(item_id: int) -> dict:
        """Obtiene el detalle de un elemento del catálogo dado su `id`.

        Lanza un error si el id no existe.
        """
        for item in _load():
            if item["id"] == item_id:
                return item
        raise ValueError(f"No existe un elemento con id={item_id}")

    @mcp.tool()
    def search_catalogo(query: str) -> list[dict]:
        """Busca elementos del catálogo usando una mini-DSL de filtros.

        La query es una o más cláusulas `campo<operador>valor` separadas por
        espacios (funcionan como un AND). Campos disponibles: `tipo`,
        `region`, `valor`. Operadores: `=`, `>`, `<`, `>=`, `<=`.

        Ejemplos funcionales:
         - Todo lo de tipo aire: `tipo=aire`
         - Sensores de aire en la región Norte: `tipo=aire region=Norte`
         - Estaciones de clima con valor mayor a 25: `tipo=clima valor>25`
         - Calidad de aire mala (AQI alto): `tipo=aire valor>=50`
        """
        clauses = _CLAUSE_RE.findall(query)
        if not clauses:
            raise ValueError(
                "Query vacía o con formato inválido. Usa cláusulas como "
                "'tipo=aire' o 'valor>25', separadas por espacios."
            )
        results = []
        for item in _load():
            if all(_matches(item, field, op, value) for field, op, value in clauses):
                results.append(item)
        return results

    @mcp.tool()
    def resumen_por_tipo() -> list[dict]:
        """Agrega el catálogo por `tipo` y devuelve conteo y promedio de `valor`.

        Útil para responder preguntas como "¿cuántos sensores de aire hay?"
        o "¿cuál es el promedio de temperatura?" sin tener que listar y
        sumar manualmente en el prompt.
        """
        by_type: dict[str, list[float]] = {}
        for item in _load():
            by_type.setdefault(item["tipo"], []).append(item["valor"])
        return [
            {"tipo": tipo, "conteo": len(valores), "promedio": round(sum(valores) / len(valores), 2)}
            for tipo, valores in sorted(by_type.items())
        ]
