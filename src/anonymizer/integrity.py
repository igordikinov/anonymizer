"""Exact, disk-backed integrity checks against the files actually exported."""
from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager

from . import canonical
from .connectivity import domain_value
from .errors import AnonymizerError, Cancelled
from .export_readback import read_csv_output, read_sql_output, MAX_RECORD
from .metadata import SYSTEM
from .validation import validate_value

MAX_METADATA = 16 * 1024**2


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _reason(value, fallback):
    # Reasons never contain a source schema, value or exception message.
    return value if isinstance(value, str) and value and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789" for c in value) else fallback


def _check_result(descriptor, kind):
    status = descriptor.get("status", "PENDING")
    if status not in {"PENDING", "NOT_CHECKED", "UNSUPPORTED"}:
        status = "NOT_CHECKED"
    result = {"id": descriptor["id"], "kind": kind, "status": status,
              "required": bool(descriptor.get("required", False))}
    if status != "PENDING":
        result["reason"] = _reason(descriptor.get("reason"), "CHECK_UNSUPPORTED" if status == "UNSUPPORTED" else "CHECK_NOT_APPLICABLE")
    return result


class IntegrityVerifier:
    """Private SQLite sidecar; ownership of the Store connection stays outside."""
    def __init__(self, store, contract, check=lambda: None, progress=lambda amount, message: None):
        self.store, self.contract, self.check, self.db = store, contract, check, store.db
        self.tables = store.tables
        self.ordinals = {name: 0 for name in self.tables}
        self.by_id = {table["id"]: name for name, table in self.tables.items()}
        self.lookup = {name: {field["name"]: (i, field) for i, field in enumerate(table["fields"])}
                       for name, table in self.tables.items()}
        metadata = {"domains": contract.domains, "relations": contract.relations,
                    "keys": contract.keys, "required": contract.required_checks}
        if len(_json(metadata).encode("utf-8")) > MAX_METADATA:
            raise AnonymizerError("CHECK_INCOMPLETE: превышен лимит метаданных проверки.")
        self.db.executescript("""
            PRAGMA temp_store=FILE;
            CREATE TABLE integrity_rows(tid INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                rid INTEGER NOT NULL UNIQUE, old TEXT NOT NULL, planned TEXT NOT NULL, actual TEXT,
                PRIMARY KEY(tid,ordinal)) WITHOUT ROWID;
            CREATE INDEX integrity_tid_rid ON integrity_rows(tid,rid);
        """)
        self.checks = []
        self._progress_error = None
        self.progress = progress

    def _notify(self, amount, message):
        self.check()
        self.progress(amount, message)
        self.check()

    def expected_row(self, name, row_id, old, new):
        self.check()
        table = self.tables[name]
        fields = table["fields"]
        # A transformer may mutate its argument in-place. Only the private
        # input spool, never that argument or the planned result, is the oracle.
        source = self.db.execute("SELECT body FROM rows WHERE id=? AND tid=?", (row_id, table["id"])).fetchone()
        if source is None:
            raise AnonymizerError("CHECK_INCOMPLETE: исходная строка проверки отсутствует.")
        old = json.loads(source[0])
        if len(old) != len(fields) or len(new) != len(fields):
            raise AnonymizerError("CHECK_INCOMPLETE: неправильная ширина строки проверки.")
        encoded_old, encoded_new = _json(old), _json(new)
        if max(len(encoded_old.encode("utf-8")), len(encoded_new.encode("utf-8"))) > MAX_RECORD:
            raise AnonymizerError("CHECK_INCOMPLETE: превышен лимит строки проверки.")
        self.ordinals[name] += 1
        self.db.execute("INSERT INTO integrity_rows(tid,ordinal,rid,old,planned) VALUES (?,?,?,?,?)",
                        (self.tables[name]["id"], self.ordinals[name], row_id, encoded_old, encoded_new))

    @contextmanager
    def _progress(self):
        def handler():
            try:
                self.check()
            except BaseException as error:
                self._progress_error = error
                return 1
            return 0
        self.db.set_progress_handler(handler, 1000)
        try:
            yield
        except sqlite3.OperationalError:
            if self._progress_error is not None:
                raise self._progress_error
            raise
        finally:
            self.db.set_progress_handler(None, 0)

    def verify(self, directory, schema_names):
        total = 3 + len(self.contract.domains) + len(self.contract.relations) + len(self.contract.keys)
        completed = 0
        with self._progress():
            readers = (
                ("csv", "CSV", lambda emit: read_csv_output(directory / "csv", self.tables, schema_names, emit, self.check)),
                ("dump", "COPY", lambda emit: read_sql_output(directory / "dump.sql", self.tables, schema_names, emit, check=self.check)),
                ("inserts", "INSERT", lambda emit: read_sql_output(directory / "inserts.sql", self.tables, schema_names, emit, inserts=True, check=self.check)),
            )
            for artifact, label, reader in readers:
                self._notify(completed / total, f"Сверка экспорта {label}")
                expected = max(sum(self.ordinals.values()), 1)

                def row_progress(count):
                    self._notify((completed + min(count / expected, 1)) / total,
                                 f"Сверка {label}: прочитано строк {count:,}")

                self._read_artifact(artifact, reader, row_progress)
                completed += 1
                self._notify(completed / total, f"Сверка экспорта {label} завершена")
            if any(item["status"] == "FAIL" for item in self.checks):
                for kind, descriptors in (("domain", self.contract.domains), ("relation", self.contract.relations), ("key", self.contract.keys)):
                    for descriptor in descriptors:
                        result = _check_result(descriptor, kind)
                        if result["status"] == "PENDING":
                            result.update(status="NOT_CHECKED", reason="EXPORT_READBACK_FAILED")
                        self.checks.append(result)
                return self._summary()
            for label, descriptors, verify in (("доменов", self.contract.domains, self._domain),
                                               ("связей", self.contract.relations, self._relation),
                                               ("ключей", self.contract.keys, self._key)):
                for index, descriptor in enumerate(descriptors, 1):
                    self._notify(completed / total, f"Проверка {label}: {index} из {len(descriptors)}")
                    self.checks.append(verify(descriptor))
                    completed += 1
                    self._notify(completed / total, f"Проверка {label}: завершено {index} из {len(descriptors)}")
            return self._summary()

    def _read_artifact(self, artifact, read, progress=lambda count: None):
        counters = {name: 0 for name in self.tables}
        mismatch, source_mismatch = 0, 0
        rows_seen = 0

        def emit(name, row):
            nonlocal mismatch, source_mismatch, rows_seen
            self.check()
            rows_seen += 1
            if rows_seen % 1000 == 0:
                progress(rows_seen)
            if name not in self.tables:
                mismatch += 1
                return
            table = self.tables[name]
            counters[name] += 1
            if len(row) != len(table["fields"]):
                mismatch += 1
                return
            found = self.db.execute("SELECT rid,old,planned FROM integrity_rows WHERE tid=? AND ordinal=?",
                                    (table["id"], counters[name])).fetchone()
            if found is None:
                mismatch += 1
                return
            rid, old_body, planned = found
            encoded = _json(row)
            if len(encoded.encode("utf-8")) > MAX_RECORD:
                raise AnonymizerError("CHECK_INCOMPLETE: превышен лимит строки проверки.")
            row_mismatch = encoded != planned
            old = json.loads(old_body)
            for field, before, after in zip(table["fields"], old, row):
                validate_value(name[1], field, after)
                column = field["name"]
                if column == "sys_id" and after is not None:
                    row_mismatch = True
                elif column == "change_author" and after != "snp":
                    row_mismatch = True
                elif column == "update_date_time" and before != after:
                    source_mismatch += 1
                elif column not in SYSTEM:
                    assigned = (name[0], name[1], column) in self.contract.assignments
                    # The planned row is only a mapper result, not an oracle for
                    # source invariants: singleton UNIQUE groups can hide these
                    # changes even when all three exports match that plan.
                    source_changed = (before is None) != (after is None)
                    if field["type"] == "TEXT" and not assigned:
                        source_changed |= (before == "") != (after == "")
                    must_preserve = field["type"] in {"DATE", "DATE_TIME", "TIME", "BOOLEAN"} or column in {"datasetid", "period"}
                    if field["type"] in {"NUMBER", "INTEGER"} and not assigned:
                        must_preserve |= bool(self.contract.profile is not None or self.store.bypass_model)
                    source_changed |= must_preserve and before != after
                    source_mismatch += source_changed
            mismatch += row_mismatch
            if artifact == "csv":
                self.db.execute("UPDATE integrity_rows SET actual=? WHERE tid=? AND ordinal=?", (encoded, table["id"], counters[name]))

        try:
            read(emit)
        except Cancelled:
            raise
        except (AnonymizerError, UnicodeError, ValueError, TypeError, KeyError, StopIteration, csv.Error):
            # Actual data is never copied into the diagnostic message.
            self.checks.append({"id": "export:" + artifact, "kind": "export", "status": "FAIL", "required": True,
                                "reason": "EXPORT_READBACK_INVALID", "lost": 0, "added": 0})
            return
        count_error = sum(abs(counters[name] - self.ordinals[name]) for name in self.tables)
        expected_error = sum(abs(self.ordinals[name] - table["rows"]) for name, table in self.tables.items())
        failures = mismatch + source_mismatch + count_error + expected_error
        self.checks.append({"id": "export:" + artifact, "kind": "export", "status": "FAIL" if failures else "PASS",
                            "required": True, "unit": "rows", "before_count": sum(self.ordinals.values()), "after_count": sum(counters.values()),
                            "changed_rows": mismatch, "preserved_value_changes": source_mismatch,
                            "lost": count_error + expected_error, "added": 0,
                            **({"reason": "EXPORT_VALUES_CHANGED"} if failures else {})})

    def _binding(self, members):
        if not members:
            return False, "RELATION_TARGET_MISSING"
        for member in members:
            name = tuple(member["table"])
            table = self.tables.get(name)
            columns = member.get("columns", [member.get("column")])
            if SYSTEM.intersection(columns):
                return False, "SYSTEM_FIELD_EXCLUDED"
            if table is None or any(c not in table.get("physical_columns", {f["name"] for f in table["fields"]}) for c in columns):
                return False, "RELATION_TARGET_MISSING"
        return True, None

    def _rows(self, name):
        tid = self.tables[name]["id"]
        for rid, old, actual in self.db.execute("SELECT rid,old,actual FROM integrity_rows WHERE tid=? ORDER BY rid", (tid,)):
            self.check()
            yield rid, json.loads(old), json.loads(actual)

    def _domain(self, descriptor):
        result = _check_result(descriptor, "domain")
        partial = result["status"] == "NOT_CHECKED" and result.get("reason") in {"DOMAIN_MEMBER_MISSING", "RELATION_TARGET_MISSING"}
        if result["status"] != "PENDING" and not partial:
            return result
        members = [member for member in descriptor.get("members", []) if self._binding([member])[0]]
        partial |= len(members) != len(descriptor.get("members", []))
        if not members:
            result.update(status="NOT_CHECKED", reason="RELATION_TARGET_MISSING")
            return result
        self.db.execute("DROP TABLE IF EXISTS integrity_domain")
        self.db.execute("CREATE TEMP TABLE integrity_domain(old TEXT NOT NULL,new TEXT NOT NULL)")
        null_changes = count = invalid = 0
        for member in members:
            name = tuple(member["table"])
            index = next(i for i, f in enumerate(self.tables[name]["fields"]) if f["name"] == member["column"])
            for _, old, new in self._rows(name):
                before, after = old[index], new[index]
                if before is None or after is None:
                    null_changes += before is not after
                    continue
                try:
                    b, a = domain_value(descriptor, before), domain_value(descriptor, after)
                except AnonymizerError:
                    invalid += 1
                    continue
                self.db.execute("INSERT INTO integrity_domain VALUES (?,?)", (b, a))
                count += 1
        self.db.execute("CREATE INDEX integrity_domain_old ON integrity_domain(old,new)")
        self.db.execute("CREATE INDEX integrity_domain_new ON integrity_domain(new,old)")
        split = self.db.execute("SELECT COUNT(*) FROM (SELECT old FROM integrity_domain GROUP BY old HAVING COUNT(DISTINCT new)>1)").fetchone()[0]
        merged = self.db.execute("SELECT COUNT(*) FROM (SELECT new FROM integrity_domain GROUP BY new HAVING COUNT(DISTINCT old)>1)").fetchone()[0]
        result.update(status="FAIL" if split or merged or null_changes or invalid else "NOT_CHECKED" if partial else "PASS", unit="identity_classes", before_count=count, after_count=count,
                      lost=split, added=merged, null_changes=null_changes, invalid_values=invalid)
        if result["status"] == "FAIL":
            result["reason"] = "DOMAIN_MAPPING_CONFLICT"
        elif partial:
            result.update(partial=True, reason="DOMAIN_MEMBER_MISSING")
        self.db.execute("DROP TABLE integrity_domain")
        return result

    def _tuple(self, name, columns, row, nulls):
        lookup = self.lookup[name]
        values = [canonical.value(lookup[column][1], row[lookup[column][0]]) for column in columns]
        if nulls != "null_safe" and any(value is None for value in values):
            return None
        return _json(values)

    def _side(self, side, descriptor, nulls):
        for role, member in enumerate(descriptor["members"]):
            name = tuple(member["table"])
            for rid, old, new in self._rows(name):
                before = self._tuple(name, member["columns"], old, nulls)
                after = self._tuple(name, member["columns"], new, nulls)
                self.db.execute("INSERT INTO integrity_relation(side,member,old,new) VALUES (?,?,?,?)", (side, f"{role}:{rid}", before, after))

    def _project(self, side):
        # A SET node is represented by the minimum stable member identity.
        # Compare its full partition before reducing rows to projected nodes.
        for phase in ("old", "new"):
            self.db.execute(f"DROP TABLE IF EXISTS integrity_{phase}_groups")
            self.db.execute(f"CREATE TEMP TABLE integrity_{phase}_groups AS SELECT {phase} body,MIN(member) representative FROM integrity_relation WHERE side=? AND {phase} IS NOT NULL GROUP BY {phase}", (side,))
            self.db.execute(f"CREATE UNIQUE INDEX integrity_{phase}_body ON integrity_{phase}_groups(body)")
        changed = self.db.execute("""SELECT COUNT(*) FROM integrity_relation r
             LEFT JOIN integrity_old_groups b ON b.body=r.old LEFT JOIN integrity_new_groups a ON a.body=r.new
             WHERE r.side=? AND b.representative IS NOT a.representative""", (side,)).fetchone()[0]
        self.db.execute("""UPDATE integrity_relation SET old=CASE WHEN member=(SELECT representative FROM integrity_old_groups WHERE body=old) THEN old ELSE NULL END,
            new=CASE WHEN member=(SELECT representative FROM integrity_new_groups WHERE body=new) THEN new ELSE NULL END WHERE side=?""", (side,))
        return changed

    def _pair_count(self, phase):
        total = 0
        for left, right in self.db.execute(f"""SELECT b.n,a.n FROM
            (SELECT {phase} body,COUNT(*) n FROM integrity_relation WHERE side=0 AND {phase} IS NOT NULL GROUP BY {phase}) b
            JOIN (SELECT {phase} body,COUNT(*) n FROM integrity_relation WHERE side=1 AND {phase} IS NOT NULL GROUP BY {phase}) a ON a.body=b.body"""):
            self.check(); total += int(left) * int(right)
        return total

    def _intersection(self):
        total = 0
        for left, right in self.db.execute("""SELECT b.n,a.n FROM
            (SELECT old,new,COUNT(*) n FROM integrity_relation WHERE side=0 AND old IS NOT NULL AND new IS NOT NULL GROUP BY old,new) b
            JOIN (SELECT old,new,COUNT(*) n FROM integrity_relation WHERE side=1 AND old IS NOT NULL AND new IS NOT NULL GROUP BY old,new) a
            ON a.old=b.old AND a.new=b.new"""):
            self.check(); total += int(left) * int(right)
        return total

    def _relation(self, descriptor):
        result = _check_result(descriptor, "relation")
        result["relation_kind"] = descriptor.get("kind", "join")
        partial = result["status"] == "NOT_CHECKED" and result.get("reason") in {"RELATION_TARGET_MISSING", "COLUMN_MISSING", "PHYSICAL_COLUMN_MISSING"}
        if result["status"] != "PENDING" and not partial:
            return result
        sides = {}
        for side in ("source", "target"):
            original = descriptor.get(side, {})
            members = [member for member in original.get("members", []) if self._binding([member])[0]]
            if not members:
                result.update(status="NOT_CHECKED", reason="RELATION_TARGET_MISSING")
                return result
            partial |= len(members) != len(original.get("members", []))
            sides[side] = {**original, "members": members}
        self.db.execute("DROP TABLE IF EXISTS integrity_relation")
        self.db.execute("CREATE TEMP TABLE integrity_relation(side INTEGER NOT NULL,member TEXT NOT NULL,old TEXT,new TEXT,PRIMARY KEY(side,member)) WITHOUT ROWID")
        self._side(0, sides["source"], descriptor.get("nulls", "sql"))
        self._side(1, sides["target"], descriptor.get("nulls", "sql"))
        self.db.execute("CREATE INDEX integrity_relation_old ON integrity_relation(side,old)")
        self.db.execute("CREATE INDEX integrity_relation_new ON integrity_relation(side,new)")
        projection_changes = 0
        for side, label in ((0, "source"), (1, "target")):
            if descriptor[label].get("projection", "bag") == "set":
                projection_changes += self._project(side)
        before, after, common = self._pair_count("old"), self._pair_count("new"), self._intersection()
        lost, added = before - common, after - common
        unmatched_before = unmatched_after = unmatched_changed = 0
        if descriptor.get("kind") == "fk":
            self.db.execute("DROP TABLE IF EXISTS integrity_unmatched")
            self.db.execute("""CREATE TEMP TABLE integrity_unmatched AS SELECT b.member,
                (b.old IS NOT NULL AND NOT EXISTS(SELECT 1 FROM integrity_relation a WHERE a.side=1 AND a.old=b.old)) old_bad,
                (b.new IS NOT NULL AND NOT EXISTS(SELECT 1 FROM integrity_relation a WHERE a.side=1 AND a.new=b.new)) new_bad
                FROM integrity_relation b WHERE b.side=0""")
            unmatched_before, unmatched_after, unmatched_changed = self.db.execute("SELECT COALESCE(SUM(old_bad),0),COALESCE(SUM(new_bad),0),COALESCE(SUM(old_bad!=new_bad),0) FROM integrity_unmatched").fetchone()
        result.update(status="FAIL" if lost or added or projection_changes or unmatched_changed else "NOT_CHECKED" if partial else "PASS", unit="row_pairs",
                      before_count=before, after_count=after, lost=lost, added=added,
                      projection_membership_changes=projection_changes, source_defects=unmatched_before,
                      unmatched_before=unmatched_before, unmatched_after=unmatched_after,
                      unmatched_identity_changes=unmatched_changed)
        if partial:
            result.update(partial=True, source_defects=0)
        if result["status"] == "FAIL":
            result["reason"] = "RELATION_CHANGED"
        elif partial:
            result["reason"] = "RELATION_TARGET_MISSING"
        return result

    def _key(self, descriptor):
        result = _check_result(descriptor, "key")
        result["key_kind"] = descriptor.get("kind", "bk")
        if result["status"] != "PENDING":
            return result
        name = tuple(descriptor["table"])
        valid, reason = self._binding([{"table": name, "columns": descriptor["columns"]}])
        if not valid:
            result.update(status="NOT_CHECKED", reason=reason)
            return result
        self.db.execute("DROP TABLE IF EXISTS integrity_key")
        self.db.execute("CREATE TEMP TABLE integrity_key(rid INTEGER PRIMARY KEY,old TEXT NOT NULL,new TEXT NOT NULL)")
        source_nulls = 0
        for rid, old, new in self._rows(name):
            before = self._tuple(name, descriptor["columns"], old, "null_safe")
            after = self._tuple(name, descriptor["columns"], new, "null_safe")
            b_null = any(value is None for value in json.loads(before))
            a_null = any(value is None for value in json.loads(after))
            if descriptor.get("kind") == "pk":
                source_nulls += b_null
            distinct_nulls = descriptor.get("nulls", "distinct") == "distinct" or descriptor.get("kind") == "pk"
            before = _json(["null-row", rid]) if distinct_nulls and b_null else _json(["value", before])
            after = _json(["null-row", rid]) if distinct_nulls and a_null else _json(["value", after])
            self.db.execute("INSERT INTO integrity_key VALUES (?,?,?)", (rid, before, after))
        for phase in ("old", "new"):
            self.db.execute(f"DROP TABLE IF EXISTS integrity_key_{phase}")
            self.db.execute(f"CREATE TEMP TABLE integrity_key_{phase} AS SELECT {phase} body,MIN(rid) representative,COUNT(*) n FROM integrity_key GROUP BY {phase}")
            self.db.execute(f"CREATE UNIQUE INDEX integrity_key_{phase}_body ON integrity_key_{phase}(body)")
        changed = self.db.execute("""SELECT COUNT(*) FROM integrity_key k JOIN integrity_key_old b ON b.body=k.old
             JOIN integrity_key_new a ON a.body=k.new WHERE b.representative!=a.representative""").fetchone()[0]
        before = self.db.execute("SELECT COALESCE(SUM(n-1),0) FROM integrity_key_old WHERE n>1").fetchone()[0]
        after = self.db.execute("SELECT COALESCE(SUM(n-1),0) FROM integrity_key_new WHERE n>1").fetchone()[0]
        result.update(status="FAIL" if changed else "PASS", count_unit="extra_duplicate_rows", membership_unit="row_to_group_pairs", before_count=before, after_count=after,
                      lost=changed, added=changed, membership_changes=changed,
                      source_defects=before + source_nulls, source_nulls=source_nulls)
        if changed:
            result["reason"] = "KEY_GROUP_CHANGED"
        return result

    def _summary(self):
        declared = [item for item in self.checks if item["kind"] != "export"]
        required = set(self.contract.required_checks)
        required.update(item["id"] for item in declared if item.get("required"))
        by_id = {item["id"]: item for item in self.checks}
        failed = any(item["status"] == "FAIL" for item in self.checks)
        complete = bool(required) and all(by_id.get(identifier, {}).get("status") == "PASS" for identifier in required)
        status = "FAILED" if failed else "PASSED" if complete else "NOT_CHECKED"
        checked = [item for item in declared if item["status"] in {"PASS", "FAIL"}]
        skipped = [item for item in declared if item["status"] in {"NOT_CHECKED", "UNSUPPORTED"}]
        quality_checks = [item for item in checked if item["kind"] == "key" or item.get("relation_kind") == "fk"]
        quality_declared = [item for item in declared if item["kind"] == "key" or item.get("relation_kind") == "fk"]
        quality_complete = bool(quality_declared) and len(quality_checks) == len(quality_declared)
        source_status = ("WARNINGS" if any(item.get("source_defects", 0) for item in quality_checks)
                         else "PASSED" if quality_complete else "NOT_CHECKED")
        flags = {}
        for key, kind in (("relationships_checked", "relation"), ("keys_checked", "key")):
            subset = [item for item in declared if item["kind"] == kind]
            flags[key] = bool(subset) and all(item["status"] in {"PASS", "FAIL"} for item in subset)
        limitations = {item.get("reason", "CHECK_NOT_APPLICABLE") for item in skipped}
        if not declared:
            limitations.add("NO_DECLARED_CHECKS")
        if not required:
            limitations.add("NO_REQUIRED_CHECKS")
        return {"checks": self.checks, "coverage": {"declared": len(declared), "applicable": len(checked),
                "checked": len(checked), "skipped": len(skipped), "required": sorted(required)},
                "integrity_status": status, "source_integrity_status": source_status, **flags,
                "limitations": sorted(limitations),
                "publication_mode": None if failed else "VERIFIED_INTEGRITY" if status == "PASSED" else "UNVERIFIED_INTEGRITY",
                "dataset_published": not failed}
