"""Synthetic end-to-end XLSX input, mixed datasets and publication failures."""
import csv
import json
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from anonymizer.errors import AnonymizerError
from anonymizer.metadata import Model
from anonymizer.pipeline import Options, run
from anonymizer.sql_reader import parse_sql
from anonymizer.csv_reader import parse_csv
from anonymizer.store import Store


def write_book(path, rows):
    book = Workbook()
    sheet = book.active
    sheet.title = "Произвольный лист"
    for row in rows:
        sheet.append(row)
    book.save(path)
    book.close()
    return path


def exported_tables(output):
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    result = {}
    for entry in manifest["tables"]:
        with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
            result[entry["table"]] = list(csv.DictReader(stream))
    return result


def test_single_file_cli_suffix_repeat_and_exact_numeric(tmp_path):
    from anonymizer.cli import main
    source = write_book(tmp_path / "item_20261028123456.XLSX", [
        ["item", "proddescr", "volume"], ["001", "SYN_PRIVATE_ITEM", 1.25]])
    # Inject the exact stored numeric lexeme, bypassing Excel/Python float writers.
    with zipfile.ZipFile(source) as archive:
        parts = {entry.filename: archive.read(entry) for entry in archive.infolist()}
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(
        b"<v>1.25</v>", b"<v>12345678901234567890.123456789</v>")
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    before = source.read_bytes()
    project = tmp_path / "private.snpanon"
    for output in (tmp_path / "first", tmp_path / "second"):
        assert main(["--input", str(source), "--output", str(output), "--project", str(project),
                     "--csv-schema", "custom_input", "--delimiter", ";", "--encoding", "cp1251"]) == 0
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        assert report["rows"] == 1 and report["validation"]["preserved_fields_equal"]
        row, = exported_tables(output)["item"]
        assert row["volume"] == "12345678901234567890.123456789"
        for sql in ("dump.sql", "inserts.sql"):
            text = (output / sql).read_text(encoding="utf-8")
            assert "12345678901234567890.123456789" in text
            assert "SYN_PRIVATE_ITEM" not in text
        assert not list(output.rglob("*.xlsx")) and not list(output.rglob("*.snpanon"))
    assert source.read_bytes() == before
    assert (tmp_path / "first/dump.sql").read_bytes() == (tmp_path / "second/dump.sql").read_bytes()


@pytest.mark.parametrize("container", ["folder", "zip-flat", "zip-subfolder"])
def test_mixed_dataset_fk_three_exports_and_nulls(tmp_path, container):
    folder = tmp_path / "source"
    folder.mkdir()
    write_book(folder / "item.xlsx", [["item", "proddescr"], ["SYN_I1", "SYN_ITEM"]])
    (folder / "loc.csv").write_text("loc,locdescr\nSYN_L1,SYN_LOCATION\n", encoding="utf-8")
    write_book(folder / "sku.xlsx", [["item", "loc", "fixhor", "fixlotsize"],
                                      ["SYN_I1", "SYN_L1", None, 0]])
    source = folder
    if container.startswith("zip"):
        source = tmp_path / "dataset.zip"
        prefix = "data/" if container == "zip-subfolder" else ""
        with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in folder.iterdir():
                archive.write(file, prefix + file.name)
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "private.snpanon"))
    assert report["tables"] == report["rows"] == 3
    assert report["validation"]["preserved_fields_equal"]
    rows = exported_tables(output)
    assert rows["item"][0]["item"] == rows["sku"][0]["item"]
    assert rows["loc"][0]["loc"] == rows["sku"][0]["loc"]
    assert rows["sku"][0]["fixhor"] == r"\N" and rows["sku"][0]["fixlotsize"] == "0"
    recovered = []
    for idx, (reader, source) in enumerate(((parse_sql, output / "dump.sql"),
                                          (parse_sql, output / "inserts.sql"),
                                          (parse_csv, output / "csv"))):
        with Store(tmp_path / f"roundtrip-{idx}.sqlite", Model()) as store:
            reader(source, store)
            recovered.append({name: [[v for c, v in zip(store.columns(name), row) if c != "sys_id"]
                                     for row in store.rows(table["id"])]
                              for name, table in store.tables.items()})
    assert recovered[0] == recovered[1] == recovered[2]


@pytest.mark.parametrize("skip", [False, True])
@pytest.mark.parametrize("conflict", ["duplicate", "manifest", "sql-in-zip"])
def test_dataset_ambiguity_is_global_even_with_skip(tmp_path, skip, conflict):
    folder = tmp_path / "source"
    folder.mkdir()
    write_book(folder / "item.xlsx", [["item"], ["SYN_I1"]])
    source = folder
    if conflict == "duplicate":
        (folder / "item_20261028123456.csv").write_text("item\nSYN_I2\n", encoding="utf-8")
        match = "повторяется"
    elif conflict == "manifest":
        (folder / "manifest.json").write_text("{}", encoding="utf-8")
        match = "manifest.json"
    else:
        source = tmp_path / "source.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.write(folder / "item.xlsx", "item.xlsx")
            archive.writestr("dump.sql", "CREATE TABLE t(x int);")
        match = "одновременно"
    output = tmp_path / "out"
    with pytest.raises(AnonymizerError, match=match):
        run(Options(source, output, tmp_path / "private.snpanon", skip_error_tables=skip))
    assert not output.exists() and not list(tmp_path.glob(".snp-anon-*"))


def test_formula_skip_removes_partial_book_keeps_csv(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    write_book(folder / "item.xlsx", [["item", "proddescr"], ["SYN_I1", "SYN_TEXT"],
                                     ["SYN_I2", "=1+1"]])
    (folder / "loc.csv").write_text("loc,locdescr\nSYN_L,SYN_LOCATION\n", encoding="utf-8")
    output = tmp_path / "out"
    report = run(Options(folder, output, tmp_path / "private.snpanon", skip_error_tables=True))
    assert report["rows"] == 1 and report["tables"] == 1
    assert [t["table"] for t in report["skipped_tables"]] == ["public.item"]
    assert set(exported_tables(output)) == {"loc"}
    assert "=1+1" not in json.dumps(report)


def test_xlsx_folder_cannot_follow_a_file_outside_input(tmp_path, monkeypatch):
    folder = tmp_path / "source"
    folder.mkdir()
    book = write_book(folder / "item.xlsx", [["item"], ["SYN_I1"]])
    outside = tmp_path / "original.xlsx"
    real_resolve = Path.resolve
    def redirected_resolve(path, *args, **kwargs):
        return outside if path == book else real_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", redirected_resolve)
    with pytest.raises(AnonymizerError, match="вне папки"):
        run(Options(folder, tmp_path / "out", tmp_path / "private.snpanon", skip_error_tables=True))
    assert not (tmp_path / "out").exists()


def test_selectors_expose_xlsx_and_tabular_folder(tk_root, monkeypatch, tmp_path):
    from anonymizer.gui import Application, filedialog
    from tkinter import ttk
    for child in tk_root.winfo_children():
        child.destroy()
    app = Application(tk_root)
    selected = {}
    def file_dialog(**kwargs):
        selected["file"] = kwargs
        return str(tmp_path / "item.xlsx")
    monkeypatch.setattr(filedialog, "askopenfilename", file_dialog)
    monkeypatch.setattr(filedialog, "askdirectory", lambda **kwargs: selected.setdefault("folder", kwargs) and "")
    app.pick_source()
    app.pick_csv()
    assert app.source.get().endswith("item.xlsx")
    assert "*.xlsx" in str(selected["file"]["filetypes"])
    assert "XLSX" in selected["folder"]["title"]
    def buttons(widget):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Button):
                yield child.cget("text")
            yield from buttons(child)
    labels = list(buttons(tk_root))
    assert "Файл…" in labels and "Папка…" in labels
