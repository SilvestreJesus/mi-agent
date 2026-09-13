"""Módulo de conversión universal de CSV a JSON JUB adaptado para registro modular en FastMCP."""

from __future__ import annotations

import csv
import datetime
import json
import re
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional, Set, Tuple

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse


def normalize_str(val: Any) -> str:
    """Retorna una cadena limpia y normalizada sin espacios superfluos."""
    if val is None:
        return ""
    return str(val).strip()


def to_upper_snake(text: str) -> str:
    """Convierte un texto a formato UPPER_SNAKE_CASE removiendo acentos y caracteres especiales."""
    if not text:
        return "DESCONOCIDO"
    trans = str.maketrans("áéíóúÁÉÍÓÚñÑüÜ", "aeiouAEIOUnNuU")
    text = text.translate(trans)
    text = re.sub(r"([a-z])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    return text.upper().strip("_")


def slugify_source_id(text: str) -> str:
    """Genera un slug legible en minúsculas a partir de un texto."""
    slug = to_upper_snake(text).lower()
    return slug or uuid.uuid4().hex[:8]


def default_source_id_from_path(path: Path) -> str:
    """Deriva un source_id automático con prefijo 'src_' a partir del archivo CSV."""
    stem = path.stem
    if stem.startswith("temp_"):
        stem = stem[len("temp_"):]
    return f"src_{slugify_source_id(stem)}"


def alias(value: str, value_type: str = "STRING", description: str = "") -> dict:
    """Estructura para CatalogItemAlias."""
    return {"value": str(value), "value_type": value_type, "description": description}


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
    """Crea una entidad CatalogItem siguiendo la especificación JUB."""
    return {
        "catalog_item_id": value,
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
    catalog_id: str,
    name: str,
    value: str,
    catalog_type: str,
    description: str = "",
    items: Optional[list[dict]] = None,
) -> dict:
    """Crea una entidad Catalog con el esquema JUB."""
    return {
        "catalog_id": catalog_id,
        "name": name,
        "value": value,
        "catalog_type": catalog_type,
        "description": description,
        "items": items or [],
    }


def find_column_by_candidates(headers: List[str], candidates: List[str]) -> Optional[str]:
    """Busca en los encabezados una columna que coincida con la lista de candidatos (case-insensitive)."""
    header_map = {h.lower().strip(): h for h in headers}
    for cand in candidates:
        cand_lower = cand.lower().strip()
        if cand_lower in header_map:
            return header_map[cand_lower]
    return None


def is_numeric_value(val: Any) -> bool:
    """Determina si un valor puede ser parseado como número flotante o entero."""
    if val is None:
        return False
    s_val = str(val).strip()
    if not s_val:
        return False
    try:
        float(s_val)
        return True
    except ValueError:
        return False


def build_spatial_catalog_universal(rows: list[dict], headers: list[str]) -> tuple[dict, Optional[str], Optional[str], Optional[str]]:
    """Identifica dinámicamente columnas geográficas y construye la jerarquía espacial."""
    cve_ent_cand = ["estado_cve_ent", "cve_ent", "cve_estado", "enentidad", "cve_entidad", "entidad_cve"]
    nom_ent_cand = ["estado_nombre", "estado", "nom_ent", "enmexico", "entidad", "nom_estado", "nombre_estado"]
    cve_mun_cand = ["municipio_cve_mun", "cve_mun", "cve_municipio", "enmunicipio", "municipio_cve"]
    nom_mun_cand = ["municipio_nombre", "municipio", "nom_mun", "nom_municipio", "nombre_municipio"]

    col_cve_ent = find_column_by_candidates(headers, cve_ent_cand)
    col_nom_ent = find_column_by_candidates(headers, nom_ent_cand)
    col_cve_mun = find_column_by_candidates(headers, cve_mun_cand)
    col_nom_mun = find_column_by_candidates(headers, nom_mun_cand)

    states: dict[str, dict] = {}

    for row in rows:
        s_code = normalize_str(row.get(col_cve_ent)) if col_cve_ent else ""
        s_name = normalize_str(row.get(col_nom_ent)) if col_nom_ent else ""

        if not s_code and not s_name:
            continue

        if not s_code:
            s_code = to_upper_snake(s_name)
        else:
            s_code = s_code.zfill(2) if s_code.isdigit() else s_code

        if not s_name:
            s_name = f"Estado {s_code}"

        m_code = normalize_str(row.get(col_cve_mun)) if col_cve_mun else ""
        m_name = normalize_str(row.get(col_nom_mun)) if col_nom_mun else ""

        if m_code or m_name:
            if not m_code:
                m_code = to_upper_snake(m_name)
            else:
                m_code = m_code.zfill(3) if m_code.isdigit() else m_code
            if not m_name:
                m_name = f"Municipio {m_code}"

        if s_code not in states:
            states[s_code] = {"name": s_name, "code": s_code, "municipios": {}}

        if m_code or m_name:
            states[s_code]["municipios"][m_code] = m_name

    state_items = []
    for s_code, s_data in sorted(states.items(), key=lambda x: str(x[0])):
        s_name = s_data["name"]
        mun_items = []
        for m_code, m_name in sorted(s_data["municipios"].items(), key=lambda x: str(x[0])):
            mun_id = f"MX_{s_code}_{m_code}" if s_code.isdigit() and m_code.isdigit() else f"SPATIAL_{s_code}_{m_code}"
            mun_items.append(
                catalog_item(
                    name=m_name,
                    value=mun_id,
                    code=int(m_code) if m_code.isdigit() else 0,
                    value_type="STRING",
                    description=f"Municipio/Demarcación: {m_name}, {s_name}",
                    aliases=[
                        alias(m_code, "NUMBER" if m_code.isdigit() else "STRING", "Clave o identificador de municipio"),
                        alias(m_name, "STRING", "Nombre oficial"),
                        alias(to_upper_snake(m_name), "STRING", "Nombre en UPPER_SNAKE"),
                    ],
                )
            )

        state_id = f"MX_{s_code}" if s_code.isdigit() else f"SPATIAL_{s_code}"
        state_items.append(
            catalog_item(
                name=s_name,
                value=state_id,
                code=int(s_code) if s_code.isdigit() else 0,
                value_type="STRING",
                description=f"Estado/Entidad: {s_name}",
                aliases=[
                    alias(s_code, "NUMBER" if s_code.isdigit() else "STRING", "Clave o código de estado"),
                    alias(s_name, "STRING", "Nombre oficial"),
                    alias(to_upper_snake(s_name), "STRING", "Nombre en UPPER_SNAKE"),
                ],
                children=mun_items,
            )
        )

    if not state_items:
        default_item = catalog_item(
            name="Ubicación Global/Nacional",
            value="SPATIAL_DEFAULT",
            code=0,
            description="Ubicación no especificada explícitamente en las columnas del dataset",
        )
        cat_spatial = catalog("cat_spatial", "Dimensión Espacial — General", "SPATIAL_GENERIC", "spatial", "Dimensión espacial por defecto", [default_item])
    else:
        root_country = catalog_item(
            name="México",
            value="MX",
            code=0,
            description="República Mexicana",
            aliases=[alias("MEX", "STRING", "Código ISO 3166-1 alpha-3"), alias("484", "NUMBER", "Código numérico ISO")],
            children=state_items,
        )
        cat_spatial = catalog("cat_spatial", "Dimensión Espacial — Jerarquía Geográfica", "SPATIAL_MX", "spatial", "Jerarquía de áreas geográficas", [root_country])

    return cat_spatial, col_cve_ent or col_nom_ent, col_cve_mun or col_nom_mun, col_nom_ent or col_nom_mun


def build_temporal_catalog_universal(rows: list[dict], headers: list[str]) -> tuple[dict, Optional[str]]:
    """Detecta automáticamente columnas temporales (año, fecha) y genera el catálogo temporal."""
    year_cand = ["anio", "año", "year", "anios_reporte", "fecha", "date", "yfd"]
    col_year = find_column_by_candidates(headers, year_cand)

    detected_years: Set[int] = set()

    if col_year:
        for row in rows:
            val = normalize_str(row.get(col_year))
            match = re.search(r"\b(19\d\d|20\d\d)\b", val)
            if match:
                detected_years.add(int(match.group(1)))

    if not detected_years:
        current_y = datetime.datetime.now().year
        detected_years.add(current_y)

    year_items = [
        catalog_item(
            name=str(yr),
            value=f"Y{yr}",
            code=yr,
            value_type="DATETIME",
            temporal_value=datetime.datetime(yr, 1, 1, tzinfo=datetime.timezone.utc).isoformat(),
            description=f"Año de registro {yr}",
            aliases=[
                alias(str(yr), "NUMBER", "Año numérico"),
                alias(f"ANO_{yr}", "STRING", "Etiqueta en español"),
                alias(f"YEAR_{yr}", "STRING", "Etiqueta en inglés"),
            ],
        )
        for yr in sorted(detected_years)
    ]

    cat_temp = catalog(
        "cat_temporal",
        "Dimensión Temporal — Períodos de Registro",
        "TEMPORAL_ANIO",
        "temporal",
        "Años y fechas presentes en el conjunto de datos",
        year_items,
    )
    return cat_temp, col_year


def build_interest_catalogs_universal(rows: list[dict], headers: list[str], excluded_cols: Set[str]) -> tuple[list[dict], List[str]]:
    """Identifica inteligentemente columnas categóricas o de interés en CUALQUIER CSV."""
    categorical_cols = []
    total_rows = len(rows)
    
    for h in headers:
        if h in excluded_cols:
            continue
            
        col_lower = h.lower().strip()
        
        if any(term in col_lower for term in ['_id', 'id_', 'uuid', 'lat', 'lng', 'utmx', 'utmy', 'finallat', 'finallng']):
            continue

        non_empty_vals = [normalize_str(row.get(h)) for row in rows if normalize_str(row.get(h))]
        if not non_empty_vals:
            continue

        unique_vals = set(non_empty_vals)
        n_unique = len(unique_vals)
        
        is_low_cardinality = n_unique <= 100 and n_unique <= (total_rows * 0.3)
        
        numeric_count = sum(1 for v in unique_vals if is_numeric_value(v))
        is_mostly_numeric = (numeric_count / n_unique) >= 0.8 if n_unique > 0 else False

        if not is_mostly_numeric or is_low_cardinality:
            if n_unique > 1:  
                categorical_cols.append(h)

    catalogs = []
    for col in categorical_cols:
        col_snake = to_upper_snake(col)
        unique_vals = sorted(list(set(normalize_str(r.get(col)) for r in rows if normalize_str(r.get(col)))))

        items = []
        for idx, val in enumerate(unique_vals, start=1):
            item_val = f"{col_snake}_{to_upper_snake(val)}"
            items.append(
                catalog_item(
                    name=val,
                    value=item_val,
                    code=idx,
                    value_type="NUMBER" if is_numeric_value(val) else "STRING",
                    description=f"Valor categórico de la variable '{col}': {val}",
                    aliases=[
                        alias(val, "NUMBER" if is_numeric_value(val) else "STRING", "Valor original"),
                        alias(to_upper_snake(val), "STRING", "Formato UPPER_SNAKE")
                    ],
                )
            )

        cat_obj = catalog(
            catalog_id=f"cat_interest_{col_snake.lower()}",
            name=f"Dimensión de Interés — {col}",
            value=f"INTEREST_{col_snake}",
            catalog_type="interest",
            description=f"Catálogo dinámico extraído de la columna '{col}'",
            items=items,
        )
        catalogs.append(cat_obj)

    return catalogs, categorical_cols


def row_to_data_record_universal(
    row: dict,
    headers: list[str],
    source_id: str,
    col_ent: Optional[str],
    col_mun: Optional[str],
    col_year: Optional[str],
    interest_cols: List[str],
) -> dict:
    """Convierte una fila de cualquier CSV a un DataRecord JUB."""
    s_val = normalize_str(row.get(col_ent)) if col_ent else ""
    m_val = normalize_str(row.get(col_mun)) if col_mun else ""

    if s_val or m_val:
        s_code = s_val.zfill(2) if s_val.isdigit() else to_upper_snake(s_val)
        m_code = m_val.zfill(3) if m_val.isdigit() else to_upper_snake(m_val)
        if s_val.isdigit() and m_val.isdigit():
            spatial_id = f"MX_{s_code}_{m_code}"
        elif s_val.isdigit() and not m_val:
            spatial_id = f"MX_{s_code}"
        else:
            spatial_id = f"SPATIAL_{s_code}_{m_code}".strip("_")
    else:
        spatial_id = "SPATIAL_DEFAULT"

    year_val = normalize_str(row.get(col_year)) if col_year else ""
    match = re.search(r"\b(19\d\d|20\d\d)\b", year_val) if year_val else None
    yr = int(match.group(1)) if match else datetime.datetime.now().year
    temporal_id = datetime.datetime(yr, 1, 1, tzinfo=datetime.timezone.utc).isoformat()

    interest_ids = []
    for col in interest_cols:
        val = normalize_str(row.get(col))
        if val:
            item_id = f"{to_upper_snake(col)}_{to_upper_snake(val)}"
            interest_ids.append(item_id)

    numerical_interest_ids = {}
    for col, val in row.items():
        if col in interest_cols or col in [col_ent, col_mun, col_year]:
            continue
        if val is None:
            continue
            
        s_val = normalize_str(val)
        if not s_val:
            continue

        if is_numeric_value(s_val):
            col_lower = col.lower().strip()
            if any(k in col_lower for k in ["_id", "uuid", "lat", "lng", "utmx", "utmy"]):
                continue
            col_snake = to_upper_snake(col)
            try:
                numerical_interest_ids[col_snake] = float(s_val) if '.' in s_val else int(s_val)
            except ValueError:
                pass

    rec_id = row.get("_id") or row.get("id") or f"rec_{uuid.uuid4().hex[:12]}"

    return {
        "record_id": str(rec_id),
        "source_id": source_id,
        "spatial_id": spatial_id,
        "temporal_id": temporal_id,
        "interest_ids": interest_ids,
        "numerical_interest_ids": numerical_interest_ids,
        "raw_payload": dict(row),
    }


def _get_dirs() -> tuple[Path, Path]:
    base_app_dir = Path(__file__).resolve().parent.parent
    sources_dir = base_app_dir / "sources"
    data_dir = base_app_dir / "data"
    sources_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    return sources_dir, data_dir


def _resolve_input_csv(csv_path: str, sources_dir: Path) -> Path:
    cand = Path(csv_path)
    if cand.exists():
        return cand

    filename = cand.name
    clean_name = filename.replace("temp_", "") if filename.startswith("temp_") else filename

    for f in [sources_dir / filename, sources_dir / clean_name]:
        if f.exists():
            return f

    raise FileNotFoundError(f"No se encontró el archivo CSV en la ruta original ni en '{sources_dir}/'.")


def _safe_data_file(data_dir: Path, filename: str) -> Optional[Path]:
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
    """Registra las herramientas de conversión CSV universal dentro del servidor FastMCP principal."""

    @mcp.tool()
    async def analizar_csv(csv_path: str) -> str:
        """Analiza la estructura básica de cualquier archivo CSV y retorna sus columnas e inferencia de tipos."""
        sources_dir, _ = _get_dirs()
        path = _resolve_input_csv(csv_path, sources_dir)

        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            rows = list(reader)

        row_count = len(rows)
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
        """
        Convierte dinámicamente cualquier archivo CSV al formato JSON JUB en la carpeta 'data/'.
        Genera 'catalogs_<nombre>.json' y 'data_records_<nombre>.json'.
        """
        sources_dir, data_dir = _get_dirs()
        path = _resolve_input_csv(csv_path, sources_dir)

        if not source_id or not source_id.strip():
            source_id = default_source_id_from_path(path)
        else:
            source_id = source_id.strip()

        clean_stem = path.stem.replace("temp_", "") if path.stem.startswith("temp_") else path.stem

        cat_out = data_dir / f"catalogs_{clean_stem}.json"
        rec_out = data_dir / f"data_records_{clean_stem}.json"

        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            rows = list(reader)

        if not rows or not headers:
            raise ValueError("El archivo CSV está vacío o no tiene encabezados válidos.")

        cat_spatial, col_ent, col_mun, _ = build_spatial_catalog_universal(rows, headers)
        cat_temporal, col_year = build_temporal_catalog_universal(rows, headers)

        excluded_cols = set(filter(None, [col_ent, col_mun, col_year]))
        cat_interests, interest_cols = build_interest_catalogs_universal(rows, headers, excluded_cols)

        all_catalogs = [cat_spatial, cat_temporal] + cat_interests

        with open(cat_out, "w", encoding="utf-8") as f:
            json.dump(all_catalogs, f, ensure_ascii=False, indent=2)

        data_records = [
            row_to_data_record_universal(r, headers, source_id, col_ent, col_mun, col_year, interest_cols)
            for r in rows
        ]

        with open(rec_out, "w", encoding="utf-8") as f:
            json.dump(data_records, f, ensure_ascii=False, indent=2)

        # RETORNO ESTRUCTURADO EN JSON PARA QUE EL FRONTEND GENERE LOS BOTONES DE DESCARGA
        return json.dumps({
            "status": "success",
            "message": "Conversión Universal Exitosa",
            "file_catalogs": cat_out.name,
            "file_records": rec_out.name,
            "download_url_catalogs": f"/files/{cat_out.name}",
            "download_url_records": f"/files/{rec_out.name}",
            "summary": {
                "archivo_procesado": path.name,
                "source_id": source_id,
                "total_registros": len(data_records),
                "total_catalogos": len(all_catalogs),
                "columnas_interes": interest_cols
            }
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def listar_archivos_generados(source_id: Optional[str] = None) -> str:
        """Lista los archivos JSON (catálogos y registros) generados en la carpeta data/."""
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