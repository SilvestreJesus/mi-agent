import asyncio
from fastmcp import FastMCP
from tools.products import register  


async def main():
    print("=== TEST MCP SERVER TOOL: crear_productos_multples ===")

    mcp = FastMCP("ProductsMCPServer")
    register(mcp)

    print("\nIngresa los parámetros para la creación de productos:")
    prod_id_base = input("ID base del producto [prod-benceno-retc]: ").strip() or "prod-benceno-retc"
    prod_name_base = input("Nombre base del producto [Benceno RETC]: ").strip() or "Benceno RETC"
    prod_desc_base = input("Descripción base [Emisiones de benceno reportadas al RETC]: ").strip() or "Emisiones de benceno reportadas al RETC."
    
    start_year_input = input("Año de inicio [2004]: ").strip()
    start_year = int(start_year_input) if start_year_input.isdigit() else 2004

    end_year_input = input("Año de fin [2014]: ").strip()
    end_year = int(end_year_input) if end_year_input.isdigit() else 2014

    params = {
        "product_id_base": prod_id_base,
        "product_name_base": prod_name_base,
        "product_description_base": prod_desc_base,
        "start_year": start_year,
        "end_year": end_year,
    }

    print("\n[MCP] Invocando tool 'crear_productos_multples'...\n")

    try:
        resultado = await mcp.call_tool("crear_productos_multples", params)
        print("--- RESPUESTA MCP ---")
        print(resultado)
    except Exception as e:
        print(f"Error durante la ejecución de la Tool MCP: {e}")


if __name__ == "__main__":
    asyncio.run(main())