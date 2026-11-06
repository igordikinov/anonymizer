"""Small, entirely synthetic connected SNP dataset shared by tests and EXE smoke."""
from __future__ import annotations

import csv
from datetime import date
import json
from pathlib import Path


TABLES = (
    "item", "loc", "sku_attributes", "mot", "tlane_mot", "tlane_prod_mot", "res",
    "res_attributes", "rescapacity", "prod_cap_req_sp", "comp_req_sp", "prodsource_sp",
    "stock_levels", "sp_demand", "sp_demand_profile", "demand_stream",
)
CORE = ("ITEM", "LOCATION", "MOT", "RESOURCE", "PRODSOURCE", "DEMAND_STREAM", "DEMAND_TYPE")
ITEM_A, ITEM_B = "7300019", "SYN_ITEM_B"
LOC_A, LOC_B = "8400011", "SYN_LOC_B"
SHARED_CODE = ITEM_A  # Same source spelling in distinct domains is intentional.
DATE_A, DATE_B = "2026-01-01", "2026-12-31"
EXACT = "-2476.700000000000000001"
DATE_FIELDS = frozenset({"date", "validfr", "validto"})
CODE_FIELDS = frozenset({"item", "comp", "loc", "locfr", "locto", "mot", "res", "prodsource", "dmdstream", "demandtype"})


def snapshot():
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / "src/anonymizer/resources/model.json").read_text(encoding="utf-8"))["tables"]


def dataset(*, optional=False):
    """Cycle, self-route, multi-location resource, digits and exact quantities."""
    rows = {
        "item": [{"item": ITEM_A, "note": "SYN_PRIVATE_A"}, {"item": ITEM_B, "note": "SYN_PRIVATE_B"}],
        "loc": [{"loc": LOC_A, "note": "SYN_PRIVATE_A"}, {"loc": LOC_B, "note": "SYN_PRIVATE_B"}],
        "sku_attributes": [{"item": ITEM_A, "loc": LOC_A}, {"item": ITEM_B, "loc": LOC_B}],
        "mot": [{"mot": SHARED_CODE, "note": "SYN_PRIVATE_A"}, {"mot": "SYN_MOT_B", "note": "SYN_PRIVATE_B"}],
        "tlane_mot": [{"locfr": LOC_A, "locto": LOC_B, "mot": SHARED_CODE},
                      {"locfr": LOC_B, "locto": LOC_A, "mot": "SYN_MOT_B"},
                      {"locfr": LOC_A, "locto": LOC_A, "mot": SHARED_CODE}],
        "tlane_prod_mot": [{"item": ITEM_A, "locfr": LOC_A, "locto": LOC_B, "mot": SHARED_CODE, "validfr": DATE_A, "validto": DATE_B},
                           {"item": ITEM_B, "locfr": LOC_B, "locto": LOC_A, "mot": "SYN_MOT_B", "validfr": DATE_A, "validto": DATE_B},
                           {"item": ITEM_A, "locfr": LOC_A, "locto": LOC_A, "mot": SHARED_CODE, "validfr": DATE_A, "validto": DATE_B}],
        "res": [{"res": SHARED_CODE, "loc": LOC_A}, {"res": SHARED_CODE, "loc": LOC_B}, {"res": "SYN_RES_B", "loc": LOC_A}],
        "res_attributes": [{"res": SHARED_CODE, "loc": LOC_A}, {"res": SHARED_CODE, "loc": LOC_B}, {"res": "SYN_RES_B", "loc": LOC_A}],
        "rescapacity": [{"res": SHARED_CODE, "loc": LOC_A, "periodid": 1}, {"res": SHARED_CODE, "loc": LOC_B, "periodid": 1},
                        {"res": "SYN_RES_B", "loc": LOC_A, "periodid": 1}],
        "prodsource_sp": [{"item": ITEM_A, "loc": LOC_A, "prodsource": SHARED_CODE},
                          {"item": ITEM_B, "loc": LOC_B, "prodsource": "SYN_PS_B"}],
        "prod_cap_req_sp": [{"item": ITEM_A, "loc": LOC_A, "prodsource": SHARED_CODE, "res": SHARED_CODE, "date": DATE_A, "periodid": 1},
                            {"item": ITEM_B, "loc": LOC_B, "prodsource": "SYN_PS_B", "res": SHARED_CODE, "date": DATE_A, "periodid": 1},
                            {"item": ITEM_A, "loc": LOC_A, "prodsource": SHARED_CODE, "res": "SYN_RES_B", "date": DATE_A, "periodid": 1}],
        "comp_req_sp": [{"item": ITEM_A, "loc": LOC_A, "prodsource": SHARED_CODE, "comp": ITEM_B},
                        {"item": ITEM_B, "loc": LOC_B, "prodsource": "SYN_PS_B", "comp": ITEM_A}],
        "stock_levels": [{"item": ITEM_A, "loc": LOC_A, "periodid": 1}, {"item": ITEM_B, "loc": LOC_B, "periodid": 1}],
        "sp_demand": [{"item": ITEM_A, "loc": LOC_A, "dmdstream": SHARED_CODE, "demandtype": "1", "periodid": 1,
                       "quantity": EXACT.replace(".", ","), "sys_id": "SYN_IGNORED_ID", "change_author": "SYN_AUTHOR", "update_date_time": "SYN_RAW_TIME"},
                      {"item": ITEM_B, "loc": LOC_B, "dmdstream": "SYN_DS_B", "demandtype": "SYN_TYPE_B", "periodid": 1,
                       "quantity": "12,50", "sys_id": None, "change_author": None, "update_date_time": ""}],
        "sp_demand_profile": [{"item": ITEM_A, "loc": LOC_A, "dmdstream": SHARED_CODE, "demandtype": "1"},
                              {"item": ITEM_B, "loc": LOC_B, "dmdstream": "SYN_DS_B", "demandtype": "SYN_TYPE_B"}],
        "demand_stream": [{"dmdstream": SHARED_CODE, "note": "SYN_PRIVATE_A"}, {"dmdstream": "SYN_DS_B", "note": "SYN_PRIVATE_B"}],
    }
    if optional:
        target_values = {"uom": "KG", "sp_calendar_rules": "SYN_CAL", "sp_resgroups": "SYN_RG"}
        for table, metadata in snapshot().items():
            if table not in rows:
                continue
            for field in metadata["fields"]:
                value = target_values.get((field.get("fk") or {}).get("table"))
                if value is not None:
                    for row in rows[table]:
                        row[field["name"]] = value
    return rows


BASE_COLUMNS = {table: tuple(rows[0]) for table, rows in dataset().items()}


def columns_for(table, rows):
    return list(dict.fromkeys(column for row in rows for column in row)) if rows else list(BASE_COLUMNS[table])


def native_value(column, value):
    if value is None:
        return None
    if column in DATE_FIELDS:
        return date.fromisoformat(value)
    if column in CODE_FIELDS and isinstance(value, str) and value.isdigit() and str(int(value)) == value:
        return int(value)
    return value


def write_dataset(directory, *, kind="csv", rows=None, optional=False, date_style=None):
    from openpyxl import Workbook

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rows = dataset(optional=optional) if rows is None else rows
    for index, (table, values) in enumerate(rows.items()):
        columns = columns_for(table, values)
        file_kind = ("xlsx" if index % 2 else "csv") if kind == "mixed" else kind
        if file_kind == "xlsx":
            book = Workbook()
            book.active.append(columns)
            for row in values:
                book.active.append([native_value(col, row.get(col)) for col in columns])
            if date_style and date_style[0] == table:
                book.active.cell(2, columns.index(date_style[1]) + 1).number_format = "yyyy-mm-dd"
            book.save(directory / f"{table}.xlsx")
            book.close()
        else:
            with (directory / f"{table}.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream, delimiter=";")
                writer.writerow(columns)
                writer.writerows([[r"\N" if row.get(col) is None else row[col] for col in columns] for row in values])
    return directory


def sql_type_for(table, column):
    from anonymizer.metadata import sql_type
    known = next((f for f in snapshot()[table]["fields"] if f["name"] == column), None)
    if known is not None:
        return sql_type(known)
    return "numeric" if column in {"quantity", "cost", "capacity"} else "text"


def write_sql(path, *, schemas=("public",), rows=None, overrides=None):
    rows = dataset() if rows is None else rows
    overrides = overrides or {}
    with Path(path).open("w", encoding="utf-8") as stream:
        for schema in schemas:
            for table, values in rows.items():
                columns = columns_for(table, values)
                types = [overrides.get((table, col), sql_type_for(table, col)) for col in columns]
                stream.write(f'CREATE TABLE "{schema}"."{table}" (' + ','.join(f'"{col}" {typ}' for col, typ in zip(columns, types)) + ');\n')
                stream.write(f'COPY "{schema}"."{table}" (' + ','.join(f'"{col}"' for col in columns) + ') FROM stdin;\n')
                for row in values:
                    encoded = []
                    for col, typ in zip(columns, types):
                        value = row.get(col)
                        if value is None:
                            encoded.append(r"\N")
                        else:
                            value = str(value)
                            if typ.startswith(("numeric", "decimal")):
                                value = value.replace(",", ".")
                            encoded.append(value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r"))
                    stream.write('\t'.join(encoded) + '\n')
                stream.write('\\.\n')
    return Path(path)


def output_rows(output):
    output = Path(output)
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    tables = {}
    for entry in manifest["tables"]:
        with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
            tables[(entry["schema"], entry["table"])] = list(csv.DictReader(stream))
    return tables, manifest


def table_rows(output, name):
    tables, _ = output_rows(output)
    rows, = [rows for (_, table), rows in tables.items() if table == name]
    return rows


def assert_business_links(output):
    tables = {name: table_rows(output, name) for name in TABLES}
    item_a, item_b = [row["item"] for row in tables["item"]]
    loc_a, loc_b = [row["loc"] for row in tables["loc"]]
    assert tables["comp_req_sp"][0]["item"] == item_a and tables["comp_req_sp"][0]["comp"] == item_b
    assert tables["comp_req_sp"][1]["comp"] == item_a
    assert tables["sp_demand"][0]["item"] == tables["stock_levels"][0]["item"] == item_a
    assert tables["tlane_mot"][0]["locfr"] == tables["tlane_prod_mot"][0]["locfr"] == loc_a
    assert tables["tlane_mot"][0]["locto"] == tables["tlane_mot"][1]["locfr"] == loc_b
    assert tables["tlane_mot"][2]["locfr"] == tables["tlane_mot"][2]["locto"] == loc_a
    assert tables["mot"][0]["mot"] == tables["tlane_prod_mot"][0]["mot"]
    resource = tables["res"][0]["res"]
    assert resource == tables["res"][1]["res"] == tables["res_attributes"][0]["res"] == tables["rescapacity"][1]["res"] == tables["prod_cap_req_sp"][0]["res"]
    assert tables["res"][0]["loc"] != tables["res"][1]["loc"]
    assert tables["prodsource_sp"][0]["prodsource"] == tables["comp_req_sp"][0]["prodsource"] == tables["prod_cap_req_sp"][0]["prodsource"]
    assert tables["demand_stream"][0]["dmdstream"] == tables["sp_demand"][0]["dmdstream"] == tables["sp_demand_profile"][0]["dmdstream"]
    assert tables["sp_demand"][0]["demandtype"] == tables["sp_demand_profile"][0]["demandtype"]
    assert len({item_a, resource, tables["mot"][0]["mot"], tables["prodsource_sp"][0]["prodsource"], tables["demand_stream"][0]["dmdstream"]}) == 5
    assert [r["quantity"] for r in tables["sp_demand"][:2]] == [EXACT, "12.50"]
    assert all(r["validfr"] == DATE_A and r["validto"] == DATE_B for r in tables["tlane_prod_mot"])
    assert all(r["date"] == DATE_A and r["periodid"] == "1" for r in tables["prod_cap_req_sp"])
    assert tables["sp_demand"][0]["sys_id"] == "" and tables["sp_demand"][0]["change_author"] == "snp"
    assert tables["sp_demand"][0]["update_date_time"] == "SYN_RAW_TIME"
    return tables
