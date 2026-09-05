import sys
import asyncio
from pathlib import Path
from fastmcp import FastMCP

root_dir = Path(__file__).resolve().parent
mcp_server_dir = root_dir / "mcp_server"

if str(mcp_server_dir) not in sys.path:
    sys.path.insert(0, str(mcp_server_dir))

from tools.csv_converter import register

async def main():
    mcp = FastMCP("TestConverterServer")
    register(mcp)

    base_data_dir = mcp_server_dir / "data"
    
    csv_file = str(base_data_dir / "emisiones_benceno.csv")
    catalogs_out = str(base_data_dir / "catalogs.json")
    records_out = str(base_data_dir / "data_records.json")

    if not Path(csv_file).exists():
        print(f"[ERROR] No se encontró el archivo CSV en la ruta esperada: {csv_file}")
        return

    user_source_id = input("Ingresa el source_id (presiona Enter para usar el nombre por defecto): ").strip()
    
    params = {
        "csv_path": csv_file,
        "catalogs_output_path": catalogs_out,
        "data_records_output_path": records_out
    }
    
    if user_source_id:
        params["source_id"] = user_source_id

    print(f"\n[INFO] Ejecutando herramienta con el archivo: {csv_file}")
    
    resultado = await mcp.call_tool("convertir_csv_a_json", params)
    print("\n[RESULTADO]:")
    print(resultado)

if __name__ == "__main__":
    asyncio.run(main())