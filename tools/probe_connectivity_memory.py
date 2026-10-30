"""Measure source-runtime connectivity RSS on growing synthetic datasets.

This is a measurement tool, not an invented memory SLA. Each size gets a fresh
process and a private temporary directory; no input values enter the report.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import psutil


def synthetic_profile():
    def side(table, columns):
        return {"members": [{"table": table, "columns": columns}], "projection": "bag"}

    return {
        "format": "snp-anonymizer-connectivity-v1", "profile_id": "synthetic-memory", "version": "1",
        "namespace_rule": "source_schema", "domains": [{
            "id": "LOCATION", "kind": "text", "prefix": "LOC", "members": [
                {"table": "nodes", "column": "code"},
                {"table": "edges", "column": "source"},
                {"table": "edges", "column": "target"}]}],
        "relations": [{"id": role, "kind": "fk", "source": side("edges", ["datasetid", role]),
                       "target": side("nodes", ["datasetid", "code"]), "nulls": "match_simple"}
                      for role in ("source", "target")],
        "keys": [
            {"id": "nodes_key", "table": "nodes", "columns": ["datasetid", "code"], "kind": "bk", "nulls": "not_distinct"},
            {"id": "edges_key", "table": "edges", "columns": ["datasetid", "source", "target", "period"], "kind": "bk", "nulls": "not_distinct"}],
        "required_checks": ["domain:LOCATION", "source", "target", "nodes_key", "edges_key"],
        "missing_target_policy": "not_checked",
    }


def child(rows: int, result: Path):
    from anonymizer.mapping import Project
    from anonymizer.pipeline import Options, run

    observed = {"mapping_cache_bytes": 0, "mapping_cache_entries": 0}
    # Observe existing caches, without adding another cache proportional to N.
    for name in tuple(vars(Project)):
        original = getattr(Project, name)
        if "pseudonym" not in name or not callable(original):
            continue

        def observed_call(self, *args, _original=original, **kwargs):
            try:
                return _original(self, *args, **kwargs)
            finally:
                observed["mapping_cache_bytes"] = max(observed["mapping_cache_bytes"], getattr(self, "cache_bytes", 0))
                observed["mapping_cache_entries"] = max(observed["mapping_cache_entries"], len(getattr(self, "cache", ())))

        setattr(Project, name, observed_call)

    with tempfile.TemporaryDirectory(prefix="snp-connectivity-memory-child-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        def code(index):
            return "SYN_NODE_" + f"{index:012d}" + "_" + "X" * 80

        with (source / "nodes.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["datasetid", "code"])
            for index in range(rows):
                writer.writerow([1, code(index)])
        with (source / "edges.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["datasetid", "source", "target", "period", "quantity"])
            for index in range(rows):
                writer.writerow([1, code(index), code((index + 1) % rows), index % 365, "1234567890.123456789"])
        started = time.perf_counter()
        report = run(Options(source, root / "out", root / "private.snpanon", bypass_model=True,
                             connectivity_profile=synthetic_profile(), require_integrity=True, delimiter=","))
        assert report["integrity_status"] == "PASSED"
        assert observed["mapping_cache_bytes"] <= 4 * 1024 * 1024
        assert observed["mapping_cache_entries"] <= 10000
        result.write_text(json.dumps({"input_rows": rows * 2, "unique_identifiers": rows,
                                      "seconds": time.perf_counter() - started,
                                      "integrity_status": report["integrity_status"],
                                      "checked": report["coverage"]["checked"], **observed}), encoding="utf-8")


def measure(sizes, output: Path):
    def runtime_digest():
        digest = hashlib.sha256()
        source = Path(__file__).resolve().parents[1] / "src/anonymizer"
        for path in sorted(source.glob("*.py")):
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    runtime_hash = runtime_digest()
    results = []
    with tempfile.TemporaryDirectory(prefix="snp-connectivity-memory-parent-") as temporary:
        root = Path(temporary)
        for count in sizes:
            result_file, error_file = root / f"{count}.json", root / f"{count}.stderr"
            with error_file.open("wb") as errors:
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", str(count),
                                            "--result", str(result_file)], stdout=subprocess.DEVNULL, stderr=errors,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                probe = psutil.Process(process.pid)
                peak = 0
                peak_single = 0
                observed_processes = {}
                while process.poll() is None:
                    try:
                        # A Windows venv executable can be a small redirector:
                        # the interpreter doing the work is its descendant.
                        members = [probe, *probe.children(recursive=True)]
                        resident = 0
                        for member in members:
                            try:
                                rss = member.memory_info().rss
                                resident += rss
                                peak_single = max(peak_single, rss)
                                if member.pid not in observed_processes:
                                    observed_processes[member.pid] = {
                                        "pid": member.pid,
                                        "executable_name": Path(member.exe()).name,
                                        "executable": member.exe(),
                                    }
                            except psutil.NoSuchProcess:
                                pass
                        peak = max(peak, resident)
                    except psutil.NoSuchProcess:
                        pass
                    time.sleep(0.02)
                exit_code = process.wait()
            if exit_code:
                raise RuntimeError(f"Synthetic memory child failed for N={count}, exit={exit_code}; "
                                   + error_file.read_text(encoding="utf-8", errors="replace")[-12000:])
            if runtime_digest() != runtime_hash:
                raise RuntimeError("Runtime sources changed during the memory measurement; rerun after integration")
            result = json.loads(result_file.read_text(encoding="utf-8"))
            result.update(peak_rss_bytes=peak, peak_rss_mib=round(peak / 1024**2, 2),
                          peak_single_process_rss_bytes=peak_single,
                          peak_single_process_rss_mib=round(peak_single / 1024**2, 2),
                          observed_processes=list(observed_processes.values()), exit_code=exit_code)
            results.append(result)
            print(json.dumps(result), flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"method": "Fresh source Python process tree per size; psutil RSS sampled every 20ms",
                                  "rss_scope": "Sum of launcher and descendant RSS; shared pages can be counted more than once (conservative, not USS)",
                                  "runtime_sha256": runtime_hash, "synthetic_only": True,
                                  "mapping_cache_caps": {"bytes": 4 * 1024 * 1024, "entries": 10000},
                                  "memory_sla": None, "results": results}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", nargs="+", type=int, default=[2000, 10000, 50000])
    parser.add_argument("--output", type=Path, default=Path("build/connectivity-memory.json"))
    parser.add_argument("--child", type=int)
    parser.add_argument("--result", type=Path)
    arguments = parser.parse_args()
    if arguments.child is not None:
        if arguments.child < 1 or arguments.result is None:
            parser.error("--child needs a positive row count and --result")
        child(arguments.child, arguments.result)
    else:
        if any(size < 1 for size in arguments.sizes):
            parser.error("Sizes must be positive")
        measure(arguments.sizes, arguments.output)
