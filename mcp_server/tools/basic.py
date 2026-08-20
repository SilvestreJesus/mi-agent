"""Tools genéricas: calculadora + notas en memoria.

Mismo patrón que jub-agent/mcp/tools/*.py: cada módulo expone una función
`register(mcp)` que define las tools como funciones internas decoradas con
`@mcp.tool()`. El docstring de cada tool es lo que el LLM lee para decidir
cuándo y cómo llamarla, así que se escribe pensando en el modelo, no solo
en el desarrollador.
"""

from __future__ import annotations

# Estado en memoria del proceso del servidor. Se reinicia cada vez que el
# contenedor/proceso se reinicia — es intencionalmente simple, el foco del
# tutorial es MCP, no persistencia.
_notas: list[dict] = []
_next_id = 1


def register(mcp) -> None:
    @mcp.tool()
    def sumar(a: float, b: float) -> float:
        """Suma dos números y devuelve el resultado."""
        return a + b

    @mcp.tool()
    def multiplicar(a: float, b: float) -> float:
        """Multiplica dos números y devuelve el resultado."""
        return a * b

    @mcp.tool()
    def crear_nota(texto: str) -> dict:
        """Crea una nota nueva con el texto dado y la guarda en memoria.

        Devuelve la nota creada, incluyendo su `id` (útil para completarla
        o referenciarla después).
        """
        global _next_id
        nota = {"id": _next_id, "texto": texto, "completada": False}
        _notas.append(nota)
        _next_id += 1
        return nota

    @mcp.tool()
    def listar_notas(solo_pendientes: bool = False) -> list[dict]:
        """Lista las notas guardadas.

        Si `solo_pendientes` es True, excluye las notas ya marcadas como
        completadas.
        """
        if solo_pendientes:
            return [n for n in _notas if not n["completada"]]
        return list(_notas)

    @mcp.tool()
    def completar_nota(nota_id: int) -> dict:
        """Marca una nota como completada dado su `id`.

        Lanza un error si no existe una nota con ese id (el agente debe
        listar las notas primero si no conoce el id).
        """
        for nota in _notas:
            if nota["id"] == nota_id:
                nota["completada"] = True
                return nota
        raise ValueError(f"No existe una nota con id={nota_id}")
