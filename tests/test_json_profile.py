"""JSON profile: formal synthetic matrix for json_data + Transformer.json_transform.

Documents the shipped semantics as executable evidence: raw numeric lexemes,
ISO dates, canonical reference keys, deterministic key replacement, structure
and order. Non-standard property names are replaced like any client text, so
tests anchor on technical keys or positions. Synthetic data only.
"""
import json

import pytest

from anonymizer.csv_reader import parse_csv
from anonymizer.errors import AnonymizerError
from anonymizer.json_data import is_date, load, signature
from anonymizer.metadata import Model
from anonymizer.pipeline import Options, run
from anonymizer.store import Store


def copy_data_lines(output):
    lines = (output / "dump.sql").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("COPY ")) + 1
    return lines[start:next(i for i, line in enumerate(lines) if line == "\\.")]


def run_json_source(tmp_path, payload):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "source.sql"
    src.write_text("CREATE TABLE public.item(item text PRIMARY KEY, payload jsonb);"
                   "INSERT INTO public.item VALUES ('SYN_ITEM','" + payload + "');", encoding="utf-8")
    output = tmp_path / "out"
    run(Options(src, output, tmp_path / "synthetic.snpanon"))
    return output


def transformed_json_text(tmp_path, payload):
    return copy_data_lines(run_json_source(tmp_path, payload))[0].split("\t")[1]


def transformed_json(tmp_path, payload):
    return json.loads(transformed_json_text(tmp_path, payload))


def test_numbers_keep_raw_lexeme_without_float(tmp_path):
    raw = transformed_json_text(tmp_path, '{"a":1,"b":1.0,"c":1e5,"d":-0,"e":1.0E+2,"f":123456789012345678901234567890.123456789}')
    for lexeme in [":1,", ":1.0,", ":1e5,", ":-0,", ":1.0E+2,", ":123456789012345678901234567890.123456789}"]:
        assert lexeme in raw  # original numeric lexemes, no float conversion
    assert "1e+05" not in raw and "100000.0" not in raw


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constants_are_rejected(tmp_path, constant):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "source.sql"
    src.write_text("CREATE TABLE public.t(p jsonb); INSERT INTO public.t VALUES ('{" + constant + "}');", encoding="utf-8")
    with pytest.raises(AnonymizerError):
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("value,dated", [
    ("2026-10-13", True), ("2026-10-13T10:30:00", True), ("2026-10-13 10:30", True),
    ("2026-13-40", False), ("20261013", False), ("13.10.2026", False), ("", False),
])
def test_iso_date_recognition(value, dated):
    assert is_date(value) is dated


def test_invalid_calendar_date_is_replaced_as_text(tmp_path):
    parsed = transformed_json(tmp_path, '{"date":"2026-13-40"}')
    assert parsed["date"] != "2026-13-40"  # "date" key kept, value is not an ISO date


def test_technical_keys_kept_and_custom_keys_replaced_deterministically(tmp_path):
    payload = '{"qty":7,"name":"SYN_NAME","SECRET_KEY":"SYN_VALUE","enabled":true}'
    first = transformed_json(tmp_path / "a", payload)
    assert {"qty", "name", "enabled"} <= set(first) and "SECRET_KEY" not in first and len(first) == 4
    assert first["qty"] == 7 and first["enabled"] is True
    assert "SYN_" not in json.dumps(first, ensure_ascii=False)
    again = transformed_json(tmp_path / "b", payload)
    assert again == first  # ordinal-based domains are deterministic across projects


def test_entity_references_map_to_the_same_domain_as_columns(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text('CREATE TABLE public.item(item text PRIMARY KEY, payload jsonb);'
                   "INSERT INTO public.item VALUES ('SYN_ITEM','{\"item\":\"SYN_ITEM\",\"description\":\"SYN_OTHER\"}');",
                   encoding="utf-8")
    output = tmp_path / "out"
    run(Options(src, output, tmp_path / "synthetic.snpanon"))
    item_value, payload = copy_data_lines(output)[0].split("\t")[:2]
    parsed = json.loads(payload)
    assert parsed["item"] == item_value  # same entity map as the primary key column
    assert parsed["description"] != "SYN_OTHER"


def test_reference_key_is_inherited_into_lists_and_nested_objects(tmp_path):
    parsed = transformed_json(tmp_path, '{"loc":["SYN_PLANT",{"loc":"SYN_PLANT"}],"data":{"res":"SYN_RES"}}')
    assert parsed["loc"][0] == parsed["loc"][1]["loc"]
    assert parsed["data"]["res"].startswith("RES")


def test_structure_order_nulls_bools_empty_survive_with_key_replacement(tmp_path):
    parsed = transformed_json(tmp_path, '{"z":1,"a":{"м":null,"f":false},"empty":{},"arr":[],"s":""}')
    assert list(parsed) == ["A000001", "A000002", "A000005", "A000006", "A000007"]  # insertion order; 3-4 = nested keys
    values = list(parsed.values())
    assert values[0] == 1
    assert list(values[1].values()) == [None, False]
    assert values[2] == {} and values[3] == [] and values[4] == ""


def test_unicode_text_value_replaced_but_valid(tmp_path):
    parsed = transformed_json(tmp_path, '{"description":"СЫРЬЁ \\u00c9\\ud83d\\ude00"}')
    assert set(parsed) == {"description"}
    assert parsed["description"] and parsed["description"] != "СЫРЬЁ É\U0001F600"


def test_json_signature_ignores_text_but_keeps_numbers_dates_structure():
    assert signature(load('{"n":1.0,"d":"2026-10-13","t":"SYN"}')) == signature(load('{"n":1.0,"d":"2026-10-13","t":"OTHER"}'))
    assert signature(load('{"n":1.0}')) != signature(load('{"n":1}'))
    assert signature(load('{"d":"2026-10-13"}')) != signature(load('{"d":"2026-10-14"}'))
    assert signature(load('{"a":1,"b":2}')) != signature(load('{"a":1,"b":2,"c":3}'))


def test_duplicate_properties_collapse_last_wins(tmp_path):
    parsed = transformed_json(tmp_path, '{"qty":1,"qty":2}')
    assert parsed == {"qty": 2}


def test_unicode_escape_decodes_before_replacement(tmp_path):
    escaped = transformed_json(tmp_path, '{"description":"\\u0410\\u0411"}')
    plain = transformed_json(tmp_path / "b", '{"description":"АБ"}')
    assert escaped["description"] == plain["description"]  # same source text after decoding


def test_replacement_colliding_with_original_is_blocked(tmp_path):
    # "A000001" is the first generated key replacement; "Attribute 0001" the first
    # text replacement. Either must block instead of leaking the original text.
    for payload in ['{"A000001":1}', '{"description":"Attribute 0001"}']:
        tmp = tmp_path / f"case{len(payload)}"
        with pytest.raises(AnonymizerError, match="совпала"):
            transformed_json(tmp, payload)
        assert not (tmp / "out").exists()


def test_broken_json_blocks_with_safe_error(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "source.sql"
    src.write_text("CREATE TABLE public.t(p jsonb); INSERT INTO public.t VALUES ('{\"SECRET_UNCLOSED');", encoding="utf-8")
    with pytest.raises(AnonymizerError):
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert not (tmp_path / "out").exists()


def test_json_in_fk_column_is_blocked(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.master(id numeric PRIMARY KEY);
      CREATE TABLE public.t(payload jsonb, FOREIGN KEY (payload) REFERENCES public.master(id));
      INSERT INTO public.master VALUES (1);
      INSERT INTO public.t VALUES ('{}');
    """, encoding="utf-8")
    with pytest.raises(AnonymizerError):
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert not (tmp_path / "out").exists()


def test_jsonb_only_arrives_from_sql_ddl():
    model = Model()
    assert all(f["type"] != "JSONB" for entry in model.tables.values() for f in entry["fields"])


def test_three_exports_agree_on_json_and_csv_roundtrip(tmp_path):
    from anonymizer.sql_reader import parse_sql
    payload = '{"item":"SYN_ITEM","qty":3,"date":"2026-10-13","data":{"SECRET":"SYN_V"}}'
    output = run_json_source(tmp_path, payload)
    dump_value = copy_data_lines(output)[0].split("\t")[1]
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    (entry,) = manifest["tables"]
    insert_store = Store(tmp_path / "inserts.sqlite", Model())
    insert_store.define((entry["schema"], entry["table"]), entry["fields"])
    parse_sql(output / "inserts.sql", insert_store)
    (insert_row,) = insert_store.rows(insert_store.tables[(entry["schema"], entry["table"])]["id"])
    insert_store.close()
    store = Store(tmp_path / "roundtrip.sqlite", Model())
    parse_csv(output / "csv", store)
    (row,) = store.rows(store.tables[(entry["schema"], entry["table"])]["id"])
    store.close()
    assert dump_value == row[1] == insert_row[1]  # all three exports carry the same JSON
    assert json.loads(row[1])["date"] == "2026-10-13"
