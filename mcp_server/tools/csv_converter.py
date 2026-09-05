"""Módulo de conversión de CSV a JSON JUB adaptado para registro modular en FastMCP."""

from __future__ import annotations

import csv
import datetime
import json
import re
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

# Columnas numéricas que son identificadores o metadatos
EXCLUDE_NUMERICAL_KEYS = {
    "ANIO",
    "AÑO",
    "YEAR",
    "SUSTANCIA_GRUPO_IARC",
    "ESTADO_CVE_ENT",
    "MUNICIPIO_CVE_MUN",
    "CVE_ENT",
    "CVE_MUN",
}



def get_column_value(row: dict, candidates: list[str], default: str = "") -> str:
    normalized_row = {k.lower().strip(): v for k, v in row.items()}
    for col in candidates:
        col_lower = col.lower().strip()
        if col_lower in normalized_row and normalized_row[col_lower] is not None:
            val = str(normalized_row[col_lower]).strip()
            if val:
                return val
    return default


def to_upper_snake(text: str) -> str:
    if not text:
        return "DESCONOCIDO"
    trans = str.maketrans("áéíóúÁÉÍÓÚñÑüÜ", "aeiouAEIOUnNuU")
    text = text.translate(trans)
    text = re.sub(r"([a-z])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    return text.upper().strip("_")


def slugify_source_id(text: str) -> str:
    """Genera un slug legible en minúsculas a partir de un texto (nombre de archivo, etc.)."""
    slug = to_upper_snake(text).lower()
    return slug or uuid.uuid4().hex[:8]


def default_source_id_from_path(path: Path) -> str:
    """Deriva un source_id automático con prefijo 'src_' a partir del nombre del archivo CSV.

    Ej: 'temp_emisiones_benceno.csv' -> 'src_emisiones_benceno'
        'Reporte Q3 2026.csv'        -> 'src_reporte_q3_2026'
    """
    stem = path.stem
    if stem.startswith("temp_"):
        stem = stem[len("temp_"):]
    return f"src_{slugify_source_id(stem)}"


def alias(value: str, value_type: str, description: str = "") -> dict:
    return {"value": value, "value_type": value_type, "description": description}


def catalog_item(
    name: str,
    value: str,
    code: int,
    value_type: str = "STRING",
    description: str = "",
    temporal_value: Optional[str] = None,
    aliases: Optional[list[dict]] = None,
    children: Optional[list[dict]] = None,
) -> dict:
    return {
        "name": name,
        "value": value,
        "code": code,
        "value_type": value_type,
        "description": description,
        "temporal_value": temporal_value,
        "aliases": aliases or [],
        "children": children or [],
    }


def catalog(
    name: str,
    value: str,
    catalog_type: str,
    description: str = "",
    items: Optional[list[dict]] = None,
) -> dict:
    return {
        "name": name,
        "value": value,
        "catalog_type": catalog_type,
        "description": description,
        "items": items or [],
    }



def build_spatial_catalog(rows: list[dict]) -> dict:
    states: dict[str, dict] = {}

    cve_ent_cols = ["estado_cve_ent", "cve_ent", "cve_estado", "enentidad"]
    nom_ent_cols = ["estado_nombre", "estado", "nom_ent", "enmexico"]
    cve_mun_cols = ["municipio_cve_mun", "cve_mun", "cve_municipio", "enmunicipio"]
    nom_mun_cols = ["municipio_nombre", "municipio", "nom_mun"]

    for row in rows:
        s_code = get_column_value(row, cve_ent_cols, "00").zfill(2)
        s_name = get_column_value(row, nom_ent_cols, f"Estado {s_code}")
        m_code = get_column_value(row, cve_mun_cols, "000").zfill(3)
        m_name = get_column_value(row, nom_mun_cols, f"Municipio {m_code}")

        if s_code not in states:
            states[s_code] = {"name": s_name, "municipios": {}}
        states[s_code]["municipios"][m_code] = m_name

    state_items = []
    for s_code, s_data in sorted(states.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
        s_name = s_data["name"]
        mun_items = []
        for m_code, m_name in sorted(s_data["municipios"].items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
            mun_items.append(
                catalog_item(
                    name=m_name,
                    value=f"MX_{s_code}_{m_code}",
                    code=int(m_code) if m_code.isdigit() else 0,
                    value_type="STRING",
                    description=f"Municipio de {m_name}, {s_name}",
                    temporal_value=None,
                    aliases=[
                        alias(m_code, "NUMBER", "Clave INEGI del municipio"),
                        alias(m_name, "STRING", "Nombre oficial"),
                        alias(to_upper_snake(m_name), "STRING", "Nombre en UPPER_SNAKE"),
                    ],
                    children=[],
                )
            )

        state_items.append(
            catalog_item(
                name=s_name,
                value=f"MX_{s_code}",
                code=int(s_code) if s_code.isdigit() else 0,
                value_type="STRING",
                description=f"Estado de {s_name}, México",
                temporal_value=None,
                aliases=[
                    alias(s_code, "NUMBER", "Clave INEGI del estado"),
                    alias(s_name, "STRING", "Nombre oficial"),
                    alias(to_upper_snake(s_name), "STRING", "Nombre en UPPER_SNAKE"),
                ],
                children=mun_items,
            )
        )

    mexico = catalog_item(
        name="México",
        value="MX",
        code=0,
        value_type="STRING",
        description="República Mexicana",
        temporal_value=None,
        aliases=[
            alias("MEX", "STRING", "Código ISO 3166-1 alpha-3"),
            alias("484", "NUMBER", "Código numérico ISO 3166-1"),
        ],
        children=state_items,
    )

    return catalog(
        name="Dimensión Espacial — México",
        value="SPATIAL_MX",
        catalog_type="SPATIAL",
        description="Jerarquía geográfica: País → Estado → Municipio (fuente: INEGI)",
        items=[mexico],
    )


def build_temporal_catalog(rows: list[dict]) -> dict:
    year_cols = ["anio", "año", "year", "anios_reporte"]
    years = set()
    for row in rows:
        val = get_column_value(row, year_cols)
        if val.isdigit():
            years.add(int(val))

    year_items = [
        catalog_item(
            name=str(year),
            value=f"Y{year}",
            code=year,
            value_type="DATETIME",
            temporal_value=datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc).isoformat(),
            description=f"Año de reporte {year}",
            aliases=[
                alias(str(year), "NUMBER", "Año como entero"),
                alias(f"AÑO_{year}", "STRING", "Etiqueta en español"),
                alias(f"YEAR_{year}", "STRING", "Etiqueta en inglés"),
            ],
        )
        for year in sorted(years)
    ]

    return catalog(
        name="Dimensión Temporal — Años de Reporte",
        value="TEMPORAL_ANIO",
        catalog_type="TEMPORAL",
        description="Años calendario presentes en los reportes",
        items=year_items,
    )


def build_sustancia_catalog(rows: list[dict]) -> dict:
    cas_cols = ["sustancia_cas", "cas"]
    name_cols = ["sustancia_nombre", "sustancia"]
    iarc_cols = ["sustancia_grupo_iarc", "iarc_group", "iarc_agent"]

    sustancias: dict[str, dict] = {}
    for row in rows:
        cas = get_column_value(row, cas_cols)
        name = get_column_value(row, name_cols)
        grupo = get_column_value(row, iarc_cols, "N/A")

        if cas or name:
            key = cas if cas else to_upper_snake(name)
            if key not in sustancias:
                sustancias[key] = {"name": name or key, "cas": cas or "SIN_CAS", "grupo_iarc": grupo}

    if not sustancias:
        return catalog("Sustancias Químicas", "SUSTANCIA_RETC", "INTEREST", "Sin sustancias detectadas", [])

    sust_items = [
        catalog_item(
            name=data["name"],
            value=to_upper_snake(data["name"]),
            code=i,
            description=f"{data['name']} — CAS {data['cas']}, Grupo IARC {data['grupo_iarc']}",
            aliases=[
                alias(data["cas"], "STRING", "Número CAS"),
                alias(f"IARC_{data['grupo_iarc']}", "STRING", "Clasificación IARC"),
            ],
        )
        for i, (key, data) in enumerate(sorted(sustancias.items()), start=1)
    ]

    return catalog(
        name="Sustancias Químicas",
        value="SUSTANCIA_RETC",
        catalog_type="INTEREST",
        description="Sustancias químicas reportadas con CAS y grupo IARC",
        items=sust_items,
    )


def row_to_data_record(row: dict, source_id: str) -> dict:
    s_code = get_column_value(row, ["estado_cve_ent", "cve_ent", "cve_estado"], "00").zfill(2)
    m_code = get_column_value(row, ["municipio_cve_mun", "cve_mun", "cve_municipio"], "000").zfill(3)
    spatial_id = f"MX_{s_code}_{m_code}"

    year_str = get_column_value(row, ["anio", "año", "year"], "2000")
    year = int(year_str) if year_str.isdigit() else 2000
    temporal_id = datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc).isoformat()

    interest_ids = []
    sustancia_nom = get_column_value(row, ["sustancia_nombre", "sustancia"])
    if sustancia_nom:
        interest_ids.append(to_upper_snake(sustancia_nom))

    cas = get_column_value(row, ["sustancia_cas", "cas"])
    if cas:
        interest_ids.append(f"CAS_{cas.replace(' ', '_')}")

    iarc = get_column_value(row, ["sustancia_grupo_iarc", "iarc_group"])
    if iarc:
        interest_ids.append(f"IARC_{iarc}")

    numerical = {}
    for col, val in row.items():
        if val is None or str(val).strip() == "":
            continue

        col_snake = to_upper_snake(col)
        if col_snake in EXCLUDE_NUMERICAL_KEYS:
            continue

        try:
            num_val = float(val)
            if not any(col.lower().endswith(suffix) for suffix in ["_id", "_cve", "cve_ent", "cve_mun", "code"]):
                numerical[col_snake] = num_val
        except ValueError:
            pass

    rec_id = row.get("_id") or f"rec_{uuid.uuid4().hex[:10]}"

    return {
        "record_id": str(rec_id),
        "source_id": source_id,
        "spatial_id": spatial_id,
        "temporal_id": temporal_id,
        "interest_ids": interest_ids,
        "numerical_interest_ids": numerical,
        "raw_payload": dict(row),
    }



def _get_dirs() -> tuple[Path, Path]:
    """Devuelve (sources_dir, data_dir), creándolos si no existen."""
    base_app_dir = Path(__file__).resolve().parent.parent
    sources_dir = base_app_dir / "sources"
    data_dir = base_app_dir / "data"
    sources_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    return sources_dir, data_dir


def _safe_data_file(data_dir: Path, filename: str) -> Optional[Path]:
    """Resuelve `filename` dentro de data_dir, evitando path traversal. None si no es válido."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    candidate = (data_dir / filename).resolve()
    data_dir_resolved = data_dir.resolve()
    if candidate != data_dir_resolved and data_dir_resolved not in candidate.parents:
        return None
    return candidate


def _listar_json(data_dir: Path, source_id_filtro: Optional[str] = None) -> list[Path]:
    archivos = sorted(p for p in data_dir.glob("*.json") if p.is_file())
    if source_id_filtro:
        filtro = slugify_source_id(source_id_filtro.replace("src_", "", 1))
        archivos = [a for a in archivos if filtro in a.stem.lower()]
    return archivos



def register(mcp: FastMCP) -> None:
    """Registra las herramientas de conversión CSV dentro del servidor FastMCP principal."""

    @mcp.tool()
    async def analizar_csv(csv_path: str) -> str:
        """Analiza la estructura básica de un archivo CSV y retorna sus columnas."""
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"El archivo CSV no existe en la ruta especificada: {csv_path}")

        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            row_count = sum(1 for _ in reader)

        headers_fmt = "\n  - ".join(headers)
        return (
            f"✓ Análisis de '{path.name}':\n"
            f"• Total de columnas: {len(headers)}\n"
            f"• Total de filas: {row_count}\n"
            f"• Columnas detectadas:\n  - {headers_fmt}"
        )

    @mcp.tool()
    async def convertir_csv_a_json(
        csv_path: str,
        source_id: Optional[str] = None,
    ) -> str:
        """Convierte dinámicamente cualquier archivo CSV ubicado en 'sources' al formato JSON en la carpeta 'data'.

        Genera SIEMPRE dos archivos, con nombres derivados del archivo CSV (no personalizables):
        'catalogs_<nombre>.json' y 'data_records_<nombre>.json'.

        Args:
            csv_path: Ruta del archivo CSV.
            source_id: Identificador para este origen de datos (ej. src_benceno2026). Es OPCIONAL:
                si no se proporciona, se genera automáticamente con el prefijo 'src_' a partir del
                nombre del archivo (ej. 'emisiones_benceno.csv' -> 'src_emisiones_benceno'). No es
                necesario detenerse a pedírselo al usuario.
        """
        path = Path(csv_path)

        sources_dir, data_dir = _get_dirs()

        target_csv_path = path
        if not path.exists() or "temp_" in path.name:
            alt_sources = sources_dir / path.name.replace("temp_", "")
            if alt_sources.exists():
                target_csv_path = alt_sources
            elif path.exists():
                target_csv_path = sources_dir / path.name.replace("temp_", "")
                with open(path, "rb") as src_f, open(target_csv_path, "wb") as dst_f:
                    dst_f.write(src_f.read())
            else:
                raise FileNotFoundError(f"No se encontró el archivo CSV en la ruta: {csv_path}")

        path = target_csv_path

        # Generar automáticamente el source_id si el usuario/agente no proporcionó uno
        if not source_id or not source_id.strip():
            source_id = default_source_id_from_path(path)
        else:
            source_id = source_id.strip()

        # Nombres de salida SIEMPRE derivados del archivo CSV (no configurables desde fuera,
        # así se evita que el modelo sobrescriba archivos previos con nombres genéricos).
        cat_out = data_dir / f"catalogs_{path.stem}.json"
        rec_out = data_dir / f"data_records_{path.stem}.json"

        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

        if not rows:
            raise ValueError("El archivo CSV está vacío o no se pudo procesar la cabecera.")

        catalogs = [
            build_spatial_catalog(rows),
            build_temporal_catalog(rows),
            build_sustancia_catalog(rows),
        ]

        with open(cat_out, "w", encoding="utf-8") as f:
            json.dump(catalogs, f, ensure_ascii=False, indent=2)

        data_records = [row_to_data_record(r, source_id) for r in rows]

        with open(rec_out, "w", encoding="utf-8") as f:
            json.dump(data_records, f, ensure_ascii=False, indent=2)

        return (
            f"✓ Procesamiento exitoso:\n"
            f"• Archivo fuente guardado/leído en: {path.resolve()}\n"
            f"• Source ID asignado: '{source_id}'\n"
            f"• Archivo de catálogos: {cat_out.name}\n"
            f"• Archivo de registros: {rec_out.name}\n"
            f"• Total de registros: {len(data_records)}"
        )

    @mcp.tool()
    async def listar_archivos_generados(source_id: Optional[str] = None) -> str:
        """Lista los archivos JSON (catálogos y registros) generados y disponibles para descargar.

        Úsala cuando el usuario pida ver, listar o descargar los JSON que se generaron.
        SIEMPRE debe listar TODOS los archivos que coincidan (normalmente son dos por cada
        conversión: uno de catálogos y otro de registros), no solo uno.

        Args:
            source_id: Opcional. Si se indica, filtra solo los archivos relacionados con ese origen.
        """
        _, data_dir = _get_dirs()
        archivos = _listar_json(data_dir, source_id)

        if not archivos:
            return (
                "No hay archivos JSON generados todavía"
                + (" para el source_id indicado." if source_id else ".")
                + " Primero convierte un CSV con `convertir_csv_a_json`."
            )

        lineas = [f"Archivos disponibles para descargar ({len(archivos)}):"]
        for a in archivos:
            lineas.append(f"- {a.name}")
        return "\n".join(lineas)


    @mcp.custom_route("/files", methods=["GET"])
    async def listar_archivos_http(request: Request):
        _, data_dir = _get_dirs()
        source_id_filtro = request.query_params.get("source_id")
        archivos = _listar_json(data_dir, source_id_filtro)
        return JSONResponse({
            "files": [{"name": a.name, "size_bytes": a.stat().st_size} for a in archivos]
        })

    @mcp.custom_route("/files/{filename}", methods=["GET"])
    async def descargar_archivo_generado(request: Request):
        filename = request.path_params["filename"]
        _, data_dir = _get_dirs()

        file_path = _safe_data_file(data_dir, filename)
        if file_path is None:
            return JSONResponse({"error": "Nombre de archivo inválido"}, status_code=400)
        if not file_path.exists() or not file_path.is_file():
            return JSONResponse({"error": "Archivo no encontrado"}, status_code=404)

        return FileResponse(
            str(file_path),
            media_type="application/json",
            filename=file_path.name,
        )



    