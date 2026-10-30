"""Atomic, data-free diagnostics for an integrity gate that refused a dataset."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from . import __version__


def profile_fields(contract):
    return {"connectivity_profile_id": getattr(contract, "profile_id", None),
            "connectivity_profile_version": getattr(contract, "version", None),
            "connectivity_profile_hash": getattr(contract, "profile_hash", None),
            "connectivity_contract_fingerprint": getattr(contract, "fingerprint", None)}


def failure_report(code, contract=None, result=None, *, bypass_model=False):
    result = dict(result or {})
    checks = list(result.get("checks", ()))
    failed = any(item.get("status") == "FAIL" for item in checks)
    if not checks:
        incomplete = code in {"CHECK_INCOMPLETE", "RELATION_TARGET_MISSING", "CHECK_UNSUPPORTED"}
        checks = [{"id": "integrity_gate", "kind": "contract", "status": "NOT_CHECKED" if incomplete else "FAIL",
                   "reason": code, "required": True}]
        failed = not incomplete
    elif code == "CHECK_INCOMPLETE" or (code == "RELATION_TARGET_MISSING" and not result.get("coverage", {}).get("required")):
        checks.append({"id": "completion_gate", "kind": "execution", "status": "NOT_CHECKED",
                       "reason": code, "required": True})
    result.update(format="snp-anonymizer-report-v2", app_version=__version__,
                  model_applied=not bypass_model,
                  structure_mode="automatic_without_model" if bypass_model else "standard",
                  integrity_status="FAILED" if failed else "NOT_CHECKED",
                  source_integrity_status=result.get("source_integrity_status", "NOT_CHECKED"),
                  relationships_checked=result.get("relationships_checked", False),
                  keys_checked=result.get("keys_checked", False),
                  checks=checks, error_code=code, publication_mode=None, dataset_published=False)
    declared = [item for item in checks if item["kind"] != "export"]
    checked = [item for item in declared if item["status"] in {"PASS", "FAIL"}]
    required = set(result.get("coverage", {}).get("required", ()))
    required.update(item["id"] for item in declared if item.get("required"))
    result["coverage"] = {"declared": len(declared), "applicable": len(checked), "checked": len(checked),
                          "skipped": len(declared) - len(checked), "required": sorted(required)}
    result.setdefault("limitations", [])
    result.update(profile_fields(contract))
    return result


def publish_diagnostic(output: Path, report):
    """The caller owns a previously non-existent output; never replace one."""
    scratch = Path(tempfile.mkdtemp(prefix=".snp-diagnostic-", dir=output.parent))
    try:
        (scratch / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        scratch.rename(output)
        return output / "report.json"
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
