import asyncio
from fastmcp import FastMCP
from tools.datasource import register


async def main():
    print("=== TEST MCP SERVER TOOL: ingresar_datasource ===")

    mcp = FastMCP("JubMCPServer")
    register(mcp)

    print("\nIngresa los datos para registrar el DataSource:")
    datasource_name = input("Nombre del DataSource [Prueba 1]: ").strip() or "Prueba 1"
    datasource_description = input("Descripción [prueba 1 con mcp]: ").strip() or "prueba 1 con mcp"

    params = {
        "datasource_name": datasource_name,
        "datasource_description": datasource_description,
    }

    print("\nInvocando tool 'ingresar_datasource'...\n")

    try:
        resultado = await mcp.call_tool("ingresar_datasource", params)
        print("--- RESPUESTA MCP ---")
        print(resultado)
    except Exception as e:
        print(f"Error durante la ejecución de la Tool MCP: {e}")


if __name__ == "__main__":
    asyncio.run(main())