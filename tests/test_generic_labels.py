"""Synthetic acceptance for column-labelled text in the full model bypass."""
import csv
import json

import pytest

from anonymizer.errors import AnonymizerError
from anonymizer.mapping import Project
from anonymizer.pipeline import Options, run


OPTIMIZER_COLUMNS = (
    "product,resource,location,bomnum,var_production_cons,period,datasetid,"
    "avaliablecapacity,capacity,firmed_production_receipts_qty,"
    "adjust_production_receipts_qty,total_fa_production_receipts_qty,cost,"
    "duration,fix_production_cons,lot_multiplier,lot_multiplier_flag,lotsize_flag,"
    "maxcapacity,maxcapacitycost,maxlotsize,maxprodcost,maxprodqty,minlotsize,"
    "minprodcost,minprodqty,softmincapacity,softmincapacitycost,nullvalue,"
    "absolutevalue,absolutecost,startdate,onevalue,absoluteprodqty,"
    "aggregated_constraint,freezehorcapacity"
).split(",")


def exported_rows(output, table=None):
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    entry, = [entry for entry in manifest["tables"] if table is None or entry["table"] == table]
    with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_optimizer_csv_has_distinct_column_labels_and_keeps_all_values(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    first = ["SYN_SHARED"] * 4 + [
        "2026-10-29" if column == "startdate" else str(index) + ".1234567890123456789"
        for index, column in enumerate(OPTIMIZER_COLUMNS[4:], start=4)
    ]
    second = ["SYN_SECOND", *first[1:]]
    file = source / "optimizer_production.csv"
    with file.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(OPTIMIZER_COLUMNS)
        writer.writerows([first, second, first])
    original_file = file.read_bytes()
    project = tmp_path / "private.snpanon"
    outputs = []
    for name in ("first", "repeat"):
        output = tmp_path / name
        report = run(Options(source, output, project, bypass_model=True))
        rows = exported_rows(output)
        assert report["rows"] == 3 and not report["model_applied"]
        assert not report["relationships_checked"] and not report["keys_checked"]
        assert report["validation"]["preserved_fields_equal"]
        assert len(OPTIMIZER_COLUMNS) == 36
        assert list(rows[0]) == OPTIMIZER_COLUMNS
        assert [rows[0][column] for column in OPTIMIZER_COLUMNS[:4]] == [
            f"{column}_000001" for column in OPTIMIZER_COLUMNS[:4]
        ]
        assert rows[1]["product"] == "product_000002"
        assert rows[1]["resource"] == rows[0]["resource"]
        assert rows[2] == rows[0]
        for row in rows:
            assert [row[column] for column in OPTIMIZER_COLUMNS[4:]] == first[4:]
        assert file.read_bytes() == original_file
        outputs.append((output / "dump.sql").read_bytes())
    assert outputs[0] == outputs[1]


def test_column_labels_do_not_infer_links_between_optimizer_tables(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "optimizer_production.csv").write_text(
        "product\nSYN_FIRST\nSYN_SHARED\n", encoding="utf-8"
    )
    (source / "optimizer_bom.csv").write_text("product\nSYN_SHARED\n", encoding="utf-8")
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=","))
    assert exported_rows(output, "optimizer_bom")[0]["product"] == "product_000001"
    assert exported_rows(output, "optimizer_production")[1]["product"] == "product_000002"
    assert not report["relationships_checked"] and not report["keys_checked"]


def test_typed_sql_labels_avoid_original_and_fit_short_fields(tmp_path):
    source = tmp_path / "source.sql"
    source.write_text(
        "CREATE TABLE public.custom (product text, resource varchar(1), "
        "quantity numeric, day date);"
        "INSERT INTO public.custom VALUES "
        "('product_000001','1',1234567890.123456789,'2026-10-29'),"
        "('SYN_SECOND','2',42,'2026-10-30');",
        encoding="utf-8",
    )
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True))
    first, second = exported_rows(output)
    assert first["product"] == "product_000002"
    assert second["product"] == "product_000003"
    assert first["resource"] == "2" and second["resource"] == "3"
    assert first["quantity"] == "1234567890.123456789"
    assert second["quantity"] == "42"
    assert first["day"] == "2026-10-29" and second["day"] == "2026-10-30"
    assert report["validation"]["preserved_fields_equal"]


def test_legacy_project_keeps_old_labels_and_adds_column_labels_stably(tmp_path):
    path = tmp_path / "private.snpanon"
    project = Project(path, "food")
    legacy = project.pseudonym(
        "generic:public:custom.product", "SYN_OLD", label="text", avoid_original=True
    )
    project.finish(True)
    assert legacy == "Attribute 0001"
    source = tmp_path / "source"
    source.mkdir()
    (source / "custom.csv").write_text(
        "product,resource\nSYN_OLD,SYN_SHARED\nSYN_NEW,SYN_SHARED\n", encoding="utf-8"
    )
    outputs = []
    for name in ("first", "repeat"):
        output = tmp_path / name
        run(Options(source, output, path, bypass_model=True))
        first, second = exported_rows(output)
        assert first["product"] == legacy
        assert second["product"] == "product_000002"
        assert first["resource"] == second["resource"] == "resource_000001"
        outputs.append((output / "dump.sql").read_bytes())
    assert outputs[0] == outputs[1]


def test_prefixed_short_field_exhaustion_does_not_insert_or_cache(tmp_path):
    project = Project(tmp_path / "private.snpanon", "food")
    domain = "generic:public:tiny.product"
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    try:
        project.db.executemany(
            "INSERT INTO mapping VALUES (?,?,?,?)",
            [(domain, f"SYN_OLD_{index}", value, index) for index, value in enumerate(alphabet)],
        )
        with pytest.raises(AnonymizerError, match="Недостаточно уникальных"):
            project.pseudonym(domain, "SYN_NEW", 1, avoid_original=True, text_prefix="product")
        assert (domain, "SYN_NEW") not in project.cache
        assert project.db.execute("SELECT count(*) FROM mapping").fetchone()[0] == 36
    finally:
        project.finish(False)
