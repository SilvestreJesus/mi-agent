import asyncio
from fastmcp import FastMCP
from tools.observatory import register


async def main():
    print("=== TEST MCP SERVER TOOL: indexar_observatorio ===")

    mcp = FastMCP("ObservatoryMCPServer")
    register(mcp)

    print("\nIngresa los datos para indizar el observatorio:")
    obs_id = input("ID del observatorio [obs_benceno_retc]: ").strip() or "obs_benceno_retc"
    obs_title = input("Título [Emisiones de Benceno RETC]: ").strip() or "Emisiones de Benceno RETC"
    obs_desc = input("Descripción [Registro de emisiones por municipio]: ").strip() or "Registro de emisiones por municipio."

    params = {
        "observatory_id": obs_id,
        "title": obs_title,
        "description": obs_desc,
    }

    print("\n[MCP] Invocando tool 'indexar_observatorio'...\n")

    try:
        resultado = await mcp.call_tool("indexar_observatorio", params)
        print("--- RESPUESTA MCP ---")
        print(resultado)
    except Exception as e:
        print(f"Error durante la ejecución de la Tool MCP: {e}")


if __name__ == "__main__":
    asyncio.run(main())