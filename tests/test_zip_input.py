"""ZIP archive as input: content routing, zip-slip and bomb guards. Synthetic data only."""
import zipfile
import struct
from pathlib import Path

import pytest

from anonymizer.errors import AnonymizerError
from anonymizer.pipeline import Options, run
from anonymizer.zip_input import MAX_UNCOMPRESSED, extract_dataset

CSV_A = "item,proddescr\nSYN_A,SECRET_ONE\nSYN_B,OTHER\n"
CSV_B = "loc,locdescr\nSYN_P,SECRET_PLANT\n"


def make_zip(tmp_path, entries):
    path = tmp_path / "dataset.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return path


def test_zip_with_flat_csv_runs_like_folder(tmp_path):
    src = make_zip(tmp_path, [("item.csv", CSV_A), ("loc.csv", CSV_B)])
    zipped = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "item.csv").write_text(CSV_A, encoding="utf-8")
    (plain / "loc.csv").write_text(CSV_B, encoding="utf-8")
    folder = run(Options(plain, tmp_path / "out2", tmp_path / "synthetic.snpanon"))
    assert zipped["rows"] == folder["rows"] == 3
    assert (tmp_path / "out/dump.sql").read_bytes() == (tmp_path / "out2/dump.sql").read_bytes()


def test_zip64_cannot_hide_large_directory_behind_small_eocd(tmp_path, monkeypatch):
    source = make_zip(tmp_path, [("item.csv", CSV_A)])
    data = source.read_bytes()
    end = data.rfind(b"PK\x05\x06")
    fields = struct.unpack_from("<4s4H2LH", data, end)
    record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0,
                         fields[4], fields[4], fields[5], fields[6])
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, end, 1)
    ordinary = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0, 0, 0, 0, 0)
    source.write_bytes(data[:end] + record + locator + ordinary)
    # The standard library uses the hidden ZIP64 record, not the zero counts.
    with zipfile.ZipFile(source) as archive:
        assert len(archive.infolist()) == 1
    monkeypatch.setattr("anonymizer.zip_input.MAX_DIRECTORY_BYTES", 1)
    with pytest.raises(AnonymizerError, match="ZIP64"):
        extract_dataset(source, tmp_path / "extract")


def test_zip_directory_count_is_checked_even_when_eocd_lies(tmp_path, monkeypatch):
    source = make_zip(tmp_path, [("item.csv", CSV_A), ("loc.csv", CSV_B)])
    data = bytearray(source.read_bytes())
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", data, end + 8, 1, 1)
    source.write_bytes(data)
    monkeypatch.setattr("anonymizer.zip_input.MAX_ENTRIES", 1)
    with pytest.raises(AnonymizerError, match="числа записей"):
        extract_dataset(source, tmp_path / "extract")


def test_zip_with_single_subfolder_and_sql_variants(tmp_path):
    src = make_zip(tmp_path, [("data/item.csv", CSV_A)])
    assert run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))["rows"] == 2
    src = make_zip(tmp_path, [("dump.sql", "CREATE TABLE public.item(k text); INSERT INTO public.item VALUES ('SYN_Z');")])
    assert run(Options(src, tmp_path / "out2", tmp_path / "synthetic.snpanon"))["rows"] == 1


@pytest.mark.parametrize("entries,match", [
    ([("item.csv", CSV_A), ("dump.sql", "CREATE TABLE t(x int);")], "одновременно"),
    ([("one.sql", "CREATE TABLE t(x int);"), ("two.sql", "CREATE TABLE u(x int);")], "несколько SQL"),
    ([("readme.txt", "SYN_NOTHING_HERE")], "нет CSV"),
])
def test_unroutable_contents_are_rejected(tmp_path, entries, match):
    src = make_zip(tmp_path, entries)
    with pytest.raises(AnonymizerError, match=match):
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("name", ["../evil.txt", "/abs/evil.csv", "C:/evil.csv", "data\\..\\evil.csv", "stream:alt/item.csv"])
def test_zip_slip_entries_are_rejected(tmp_path, name):
    src = make_zip(tmp_path, [("item.csv", CSV_A), (name, "SYN_EVIL")])
    dest = tmp_path / "extract"
    dest.mkdir()
    with pytest.raises(AnonymizerError, match="небезопасный путь"):
        extract_dataset(src, dest)
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "abs").exists()


def test_junk_entries_are_skipped_whole_not_renamed(tmp_path):
    src = make_zip(tmp_path, [("item.csv", CSV_A), (".hidden/item.csv", CSV_B), ("__MACOSX/item.csv", CSV_B),
                              ("aux.csv", "x"), ("readme.txt", "SYN_NOTE")])
    dest = tmp_path / "extract"
    dest.mkdir()
    folder = extract_dataset(src, dest)
    assert folder == dest
    assert sorted(p.name for p in dest.rglob("*.csv")) == ["item.csv"]
    assert not (dest / "aux.csv").exists() and not (dest / "readme.txt").exists()


def test_mixed_root_and_subfolder_csv_is_rejected_not_silently_truncated(tmp_path):
    src = make_zip(tmp_path, [("item.csv", CSV_A), ("data/loc.csv", CSV_B)])
    with pytest.raises(AnonymizerError, match="в корне архива, и в подпапках"):
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert not (tmp_path / "out").exists()


def test_manifest_at_root_with_deep_csv_is_rejected(tmp_path):
    src = make_zip(tmp_path, [("manifest.json", "{}"), ("data/item.csv", CSV_A)])
    with pytest.raises(AnonymizerError, match="manifest.json"):
        extract_dataset(src, tmp_path / "extract")


def test_encrypted_or_unsupported_archive_gives_safe_error(tmp_path):
    src = make_zip(tmp_path, [("item.csv", CSV_A)])
    dest = tmp_path / "extract"
    dest.mkdir()
    real_open = zipfile.ZipFile.open

    def encrypted(self, entry, *args, **kwargs):
        raise NotImplementedError("compression type 9")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(zipfile.ZipFile, "open", encrypted)
    try:
        with pytest.raises(AnonymizerError, match="неподдерживаемое"):
            extract_dataset(src, dest)
    finally:
        monkeypatch.undo()
    assert real_open  # sanity


def test_counter_guard_catches_lying_header(tmp_path, monkeypatch):
    # zipfile itself polices entry sizes/CRC, so the in-copy counter is a
    # second line of defence; force it directly through the private helper.
    src = make_zip(tmp_path, [("item.csv", CSV_A)])
    dest = tmp_path / "extract"
    dest.mkdir()
    copy_calls = {"n": 0}

    def lying_read(size):
        copy_calls["n"] += 1
        return b"SYN_CHUNK" * 64 if copy_calls["n"] == 1 else None  # more than header claimed

    real_open = zipfile.ZipFile.open

    def open_and_lie(self, entry, *args, **kwargs):
        stream = real_open(self, entry, *args, **kwargs)
        stream.read = lying_read  # ignore header, pretend the stream is endless
        return stream

    monkeypatch.setattr(zipfile.ZipFile, "open", open_and_lie)
    monkeypatch.setattr("anonymizer.zip_input.MAX_UNCOMPRESSED", 100)
    with pytest.raises(AnonymizerError, match="превышает"):
        extract_dataset(src, dest)


def test_claimed_total_over_limit_is_rejected_before_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr("anonymizer.zip_input.MAX_UNCOMPRESSED", 16)
    src = make_zip(tmp_path, [("item.csv", CSV_A), ("loc.csv", CSV_B)])
    dest = tmp_path / "extract"
    dest.mkdir()
    with pytest.raises(AnonymizerError, match="превышает 4 ГиБ"):
        extract_dataset(src, dest)
    assert not list(dest.rglob("*"))


def test_cancel_during_extraction(tmp_path):
    from threading import Event
    from anonymizer.errors import Cancelled
    src = make_zip(tmp_path, [("item.csv", CSV_A)])
    cancel = Event()
    cancel.set()
    dest = tmp_path / "extract"
    dest.mkdir()
    with pytest.raises(Cancelled):
        extract_dataset(src, dest, lambda: cancel.is_set() and (_ for _ in ()).throw(Cancelled("Обработка отменена.")))


def test_limit_constant_is_sane():
    assert MAX_UNCOMPRESSED == 4 * 1024 ** 3
