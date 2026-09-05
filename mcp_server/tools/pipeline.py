import csv
import json
import os
from pathlib import Path
from typing import Any, Dict, List
import httpx
from fastmcp import FastMCP

JUB_URL = os.environ.get("JUB_API_URL", "http://host.docker.internal:5000")
JUB_USER = os.environ.get("JUB_USERNAME", "invitado")
JUB_PASS = os.environ.get("JUB_PASSWORD", "invitado")

STATE_FILE = Path(".state.json")

CATALOG_LEVELS = {
    "SPATIAL": 0,
    "TEMPORAL": 1,
    "INTEREST": 2,
    "REFERENCE": 3,
    "OBSERVABLE": 4,
}


def _is_conflict(status_code: int, detail: str) -> bool:
    if status_code in (403, 409):
        return True
    return any(
        k in detail.lower()
        for k in ("409", "403", "already", "duplicate", "exists", "forbidden")
    )


def _flatten_items(items: List[Dict[str, Any]]):
    for item in items:
        yield item
        if "children" in item and item["children"]:
            yield from _flatten_items(item["children"])


def register(mcp: FastMCP):
    @mcp.tool(name="pipeline_integral_jub")
    async def pipeline_integral_jub(
        csv_filename: str = "data/emisiones_benceno.csv",
        observatory_id: str = "",
        title: str = "",
        description: str = "",
        datasource_name: str = "DataSource Automático",
        datasource_description: str = "",  # Descripción propia del DataSource
        source_id: str = "",  # Dinámico recibido por parámetro
        product_id_base: str = "",  # Prefijo base para los product_id
        product_name_base: str = "",  # Nombre base para los productos
        product_description_base: str = "",  # Descripción base para los productos
        start_year: int = 2004,
        end_year: int = 2014,
        overwrite: bool = False,  # Si True, actualiza lo ya existente
    ) -> str:
        """
        Ejecuta el pipeline completo de indexación a JUB de forma integral y dinámica:
        1. Valida conexión y autenticación con JUB.
        2. Registra el observatorio (o detecta que ya existe).
        3. Localiza o genera dinámicamente e ingesta los catálogos según el CSV.
        4. Registra el DataSource e ingesta registros.
        5. Crea los productos múltiples por rango de años.
        """
        if not observatory_id or not title or not description:
            return json.dumps({
                "status": "error",
                "message": "Faltan parámetros obligatorios para la indexación en JUB (observatory_id, title, description).",
            }, ensure_ascii=False, indent=2)

        steps_log: List[str] = []
        warnings: List[str] = []
        resumen: Dict[str, str] = {}

        path_csv = Path(csv_filename)
        # Si la ruta es relativa y no existe, intentar buscarla dentro de la carpeta data del proyecto
        if not path_csv.exists():
            alt_path = Path("data") / path_csv.name
            if alt_path.exists():
                path_csv = alt_path

        stem = path_csv.stem
        if stem.startswith("temp_"):
            stem = stem[len("temp_"):]

        # Rutas dinámicas basadas en el nombre del CSV procesado asegurando directorio 'data'
        data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)

        catalogs_file = data_dir / f"catalogs_{stem}.json"
        data_records_file = data_dir / f"data_records_{stem}.json"

        # Conexión HTTP hacia JUB
        steps_log.append("[1/5] Conectando con la API REST de JUB...")
        async with httpx.AsyncClient(base_url=JUB_URL, timeout=60.0) as client:
            try:
                ping_res = await client.get("/api/v2/health")
                if ping_res.status_code >= 500:
                    return json.dumps({"status": "error", "message": f"El servidor JUB en {JUB_URL} respondió con error interno."}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({
                    "status": "error",
                    "message": f"No se pudo conectar a JUB en {JUB_URL}.",
                    "detalles_tecnicos": str(e)
                }, ensure_ascii=False)

            steps_log.append("Conexión exitosa con JUB.")

            # Autenticación
            steps_log.append("[2/5] Autenticando en JUB...")
            token = None
            for ep in ["/api/v2/users/auth", "/v2/auth/login", "/auth/login"]:
                try:
                    auth_res = await client.post(ep, json={"username": JUB_USER, "password": JUB_PASS})
                    if auth_res.status_code in (200, 201):
                        data = auth_res.json()
                        token = data.get("access_token") or data.get("token") or data.get("accessToken")
                        break
                except Exception:
                    continue

            headers = {"Authorization": f"Bearer {token}"} if token else {}

            # Paso 3: Registro de Observatorio
            steps_log.append("[3/5] Registrando Observatorio e ingiriendo Catálogos...")
            obs_payload = {"observatory_id": observatory_id, "title": title, "description": description}
            obs_res = await client.post("/api/v2/observatories", json=obs_payload, headers=headers)

            if obs_res.status_code in (400, 409) or _is_conflict(obs_res.status_code, obs_res.text):
                if overwrite:
                    put_res = await client.put(f"/api/v2/observatories/{observatory_id}", json=obs_payload, headers=headers)
                    if put_res.status_code in (200, 201):
                        steps_log.append(f"El observatorio '{observatory_id}' ya existía; se actualizó (overwrite=true).")
                        resumen["observatorio"] = "ya existía — actualizado (overwrite)"
                    else:
                        resumen["observatorio"] = "ya existía — no se pudo actualizar"
                        warnings.append(f"No se pudo actualizar el observatorio '{observatory_id}'.")
                else:
                    resumen["observatorio"] = "ya existía — sin cambios"
                    warnings.append(f"El observatorio '{observatory_id}' ya estaba registrado en JUB.")
            elif obs_res.status_code >= 400:
                return json.dumps({
                    "status": "error",
                    "message": f"Error al registrar el observatorio (Código {obs_res.status_code}).",
                    "detalles": obs_res.text
                }, ensure_ascii=False, indent=2)
            else:
                resumen["observatorio"] = "creado"


            if not catalogs_file.exists() or not data_records_file.exists():
                steps_log.append("No se encontraron los archivos JSON en disco. Generándolos dinámicamente desde el CSV...")
                
                catalogos_generados = [
                    {
                        "catalog_id": f"cat_spatial_{stem}",
                        "name": f"Spatial Catalog - {stem}",
                        "catalog_type": "SPATIAL",
                        "items": []
                    },
                    {
                        "catalog_id": f"cat_interest_{stem}",
                        "name": f"Interest Catalog - {stem}",
                        "catalog_type": "INTEREST",
                        "items": []
                    }
                ]
                
                registros_generados = []
                spatial_items_seen = set()
                
                if path_csv.exists():
                    with open(path_csv, encoding="utf-8") as f_csv:
                        reader = csv.DictReader(f_csv)
                        for row in reader:
                            municipio = row.get("municipio") or row.get("Municipio") or "Desconocido"
                            spatial_id = f"loc_{municipio.lower().replace(' ', '_')}"
                            
                            if spatial_id not in spatial_items_seen:
                                spatial_items_seen.add(spatial_id)
                                catalogos_generados[0]["items"].append({
                                    "catalog_item_id": spatial_id,
                                    "value": municipio,
                                    "label": municipio
                                })

                            
                            val_raw = row.get("valor") or row.get("emisiones") or 0
                            try:
                                val_parsed = float(val_raw)
                            except (ValueError, TypeError):
                                val_parsed = 0.0

                            anio_raw = row.get("anio") or row.get("año") or start_year
                            try:
                                anio_parsed = int(anio_raw)
                            except (ValueError, TypeError):
                                anio_parsed = start_year

                            registros_generados.append({
                                "spatial_id": spatial_id,
                                "value": val_parsed,
                                "year": anio_parsed
                            })
                    
                    with open(catalogs_file, "w", encoding="utf-8") as f_cat:
                        json.dump(catalogos_generados, f_cat, indent=2, ensure_ascii=False)
                    with open(data_records_file, "w", encoding="utf-8") as f_rec:
                        json.dump(registros_generados, f_rec, indent=2, ensure_ascii=False)
                    
                    steps_log.append("¡Archivos JSON generados y guardados correctamente en disco!")
                else:
                    warnings.append(f"No se encontró el archivo CSV en '{path_csv}'. No se pudieron autogenerar los JSON.")

            # INGESTA DE CATÁLOGOS
            catalog_ids: List[str] = []
            item_index: Dict[str, str] = {}
            catalogs_created = 0
            catalogs_reused = False

            if catalogs_file.exists():
                with open(catalogs_file, encoding="utf-8") as f:
                    catalogs_data = json.load(f)

                bulk_res = await client.post("/api/v2/catalogs/bulk", json=catalogs_data, headers=headers)

                if bulk_res.status_code in (200, 201):
                    bulk_json = bulk_res.json()
                    if isinstance(bulk_json, dict):
                        catalog_ids = bulk_json.get("catalog_ids", [])
                    elif isinstance(bulk_json, list):
                        catalog_ids = [c.get("catalog_id") or c.get("id") for c in bulk_json if isinstance(c, dict)]
                    catalogs_created = len(catalog_ids)

                    for cat_id, cat_dict in zip(catalog_ids, catalogs_data):
                        cat_type = cat_dict.get("catalog_type", "INTEREST")
                        level = CATALOG_LEVELS.get(cat_type, 2)
                        await client.post(
                            f"/api/v2/observatories/{observatory_id}/catalogs",
                            json={"catalog_id": cat_id, "level": level},
                            headers=headers,
                        )
                elif _is_conflict(bulk_res.status_code, bulk_res.text):
                    steps_log.append(f"Aviso: Los catálogos para '{stem}' ya existían; se reutilizarán.")
                    try:
                        existing_res = await client.get(f"/api/v2/observatories/{observatory_id}/catalogs", headers=headers)
                        if existing_res.status_code == 200:
                            existing_json = existing_res.json()
                            if isinstance(existing_json, list):
                                catalog_ids = [
                                    c.get("catalog_id") or c.get("id")
                                    for c in existing_json
                                    if isinstance(c, dict) and (c.get("catalog_id") or c.get("id"))
                                ]
                                catalogs_reused = bool(catalog_ids)
                    except Exception:
                        pass
                else:
                    warnings.append(f"No se pudieron registrar los catálogos (código {bulk_res.status_code}): {bulk_res.text[:200]}")

                # Indexar ítems de los catálogos para mapeo rápido
                for cat_id in catalog_ids:
                    cat_res = await client.get(f"/api/v2/catalogs/{cat_id}", headers=headers)
                    if cat_res.status_code == 200:
                        for item in _flatten_items(cat_res.json().get("items", [])):
                            val = item.get("value")
                            item_id = item.get("catalog_item_id") or item.get("id")
                            if val and item_id:
                                item_index[val] = item_id

                if catalogs_reused:
                    resumen["catalogos"] = f"ya existían — se reutilizarán {len(catalog_ids)}"
                elif catalogs_created:
                    resumen["catalogos"] = f"creados {catalogs_created}"
                else:
                    resumen["catalogos"] = "sin catálogos disponibles"
            else:
                resumen["catalogos"] = "archivo de catálogos no encontrado"

            # Paso 4: Registro de DataSource e Ingesta de Registros
            steps_log.append("[4/5] Registrando DataSource e ingiriendo registros dinámicos...")

            resolved_source_id = source_id.strip() if source_id else None
            if not resolved_source_id:
                list_res = await client.get("/api/v2/datasources", headers=headers)
                if list_res.status_code == 200:
                    for ds in list_res.json():
                        if ds.get("name") == datasource_name:
                            resolved_source_id = ds.get("source_id") or ds.get("id")
                            break

            if not resolved_source_id:
                resolved_source_id = f"src_{stem}"

            resolved_ds_description = datasource_description.strip() if datasource_description else f"DataSource para {title}"

            ds_payload = {
                "source_id": resolved_source_id,
                "name": datasource_name,
                "description": resolved_ds_description,
                "format": "csv",
                "connection_uri": f"file://{csv_filename}",
            }
            ds_res = await client.post("/api/v2/datasources", json=ds_payload, headers=headers)
            if ds_res.status_code in (200, 201):
                resumen["datasource"] = f"creado ({resolved_source_id})"
            elif _is_conflict(ds_res.status_code, ds_res.text):
                if overwrite:
                    await client.put(f"/api/v2/datasources/{resolved_source_id}", json=ds_payload, headers=headers)
                    resumen["datasource"] = f"ya existía — actualizado ({resolved_source_id})"
                else:
                    resumen["datasource"] = f"ya existía — reutilizado ({resolved_source_id})"
            else:
                resumen["datasource"] = f"error al registrar ({ds_res.status_code})"

            records_count = 0
            records_status = "no procesados"
            if resolved_source_id and data_records_file.exists():
                with open(data_records_file, encoding="utf-8") as f:
                    records = json.load(f)

                for rec in records:
                    rec["source_id"] = resolved_source_id
                    rec["spatial_id"] = item_index.get(rec.get("spatial_id"), rec.get("spatial_id"))
                    if "interest_ids" in rec:
                        rec["interest_ids"] = [
                            item_index.get(iid, iid) for iid in rec.get("interest_ids", [])
                        ]

                ingest_res = await client.post(f"/api/v2/datasources/{resolved_source_id}/records", json=records, headers=headers)
                if ingest_res.status_code in (200, 201):
                    records_count = len(records)
                    records_status = f"{records_count} registros ingeridos"
                elif _is_conflict(ingest_res.status_code, ingest_res.text):
                    records_status = "ya existían — no se reingresaron"
                else:
                    records_status = f"error al ingerir (código {ingest_res.status_code})"
            else:
                records_status = "archivo de registros no encontrado"

            resumen["registros"] = records_status

            # Guardar estado local (.state.json)
            state = {
                "observatory_id": observatory_id,
                "source_id": resolved_source_id,
                "item_index": item_index,
                "csv_filename": csv_filename
            }
            STATE_FILE.parent.mkdir(exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)

            # Paso 5: Creación de Productos Múltiples
            steps_log.append("[5/5] Registrando productos múltiples por rango de años...")

            resolved_prod_id_base = product_id_base.strip() if product_id_base else resolved_source_id
            resolved_prod_name_base = product_name_base.strip() if product_name_base else title
            resolved_prod_desc_base = product_description_base.strip() if product_description_base else description

            entrada_payload = {
                "product_id": f"{resolved_prod_id_base}-dataset",
                "name": f"Dataset {resolved_prod_name_base} {start_year}-{end_year}",
                "description": f"Dataset completo de {resolved_prod_desc_base}",
                "observatory_id": observatory_id,
                "catalog_item_ids": []
            }
            await client.post("/api/v2/products", json=entrada_payload, headers=headers)

            products_created: List[str] = []
            products_existing: List[str] = []
            products_failed: List[str] = []

            for year in range(start_year, end_year + 1):
                prod_id = f"{resolved_prod_id_base}-{year}"
                prod_payload = {
                    "product_id": prod_id,
                    "name": f"{resolved_prod_name_base} — {year}",
                    "description": f"{resolved_prod_desc_base} - Periodo {year}",
                    "observatory_id": observatory_id,
                    "catalog_item_ids": []
                }
                p_res = await client.post("/api/v2/products", json=prod_payload, headers=headers)
                if p_res.status_code in (200, 201):
                    products_created.append(prod_id)
                elif _is_conflict(p_res.status_code, p_res.text):
                    products_existing.append(prod_id)
                else:
                    products_failed.append(prod_id)

            resumen["productos"] = f"{len(products_created)} creados, {len(products_existing)} ya existían, {len(products_failed)} fallidos"
            steps_log.append("Pipeline completado.")

            return json.dumps({
                "status": "success" if not products_failed else "success_con_errores_parciales",
                "steps_completed": steps_log,
                "resumen": resumen,
                "advertencias": warnings,
                "observatory_id": observatory_id,
                "source_id": resolved_source_id,
                "mensaje": "¡Indexación completa y datos guardados correctamente!",
            }, ensure_ascii=False, indent=2)