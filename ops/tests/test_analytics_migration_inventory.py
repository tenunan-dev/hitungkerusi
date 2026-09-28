"""RED/GREEN tests for the Phase 3.1 inventory generator.

These use only temporary trees.  They must never manufacture a production
inventory or inspect a sibling domain.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import signal
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

import migration.build_analytics_migration_inventory as inventory_module
from migration.build_analytics_migration_inventory import (
    InventoryError,
    build_documents,
    GENERATED_OUTPUT_FILENAMES,
    inventory_tree,
    main,
    validate_document,
    write_documents,
)

# macOS commonly exposes /var as a compatibility symlink.  These safety tests
# intentionally use the physical temporary root so source ancestors are also
# opened with O_NOFOLLOW.
tempfile.tempdir = "/private/tmp"


class AnalyticsMigrationInventoryTests(unittest.TestCase):
    def test_phase32_classifies_non_code_with_exact_counterpart_and_gates(self) -> None:
        """Task 3.2/3.3 rules are fixture-tested before real sibling scanning."""
        with tempfile.TemporaryDirectory() as temporary:
            fixture = pathlib.Path(temporary)
            hermes = fixture / "HERMES"
            data = fixture / "1_DATA"
            outputs = fixture / "3_OUTPUTS" / "releases" / "O-20260906-01" / "artifacts"
            for path in (hermes, data, outputs):
                path.mkdir(parents=True)
            (hermes / "01_RESEARCH").mkdir()
            (hermes / "01_RESEARCH" / "same.csv").write_text("same\n", encoding="utf-8")
            (data / "canonical.csv").write_text("same\n", encoding="utf-8")
            (hermes / "01_RESEARCH" / "figures").mkdir()
            (hermes / "01_RESEARCH" / "figures" / ".model_cache").mkdir()
            (hermes / "01_RESEARCH" / "figures" / ".model_cache" / "cache.bin").write_bytes(b"x")
            (hermes / "03_REPORTS").mkdir()
            (hermes / "03_REPORTS" / "latest.py").write_text("pass\n", encoding="utf-8")
            from migration.build_analytics_migration_inventory import build_phase32_documents
            documents = build_phase32_documents(hermes, data, outputs)
            files = {x["path"]: x for x in documents["rename_disposition"]["files"]}
            self.assertEqual(files["01_RESEARCH/same.csv"]["classification"], "duplicate")
            self.assertEqual(files["01_RESEARCH/same.csv"]["counterpart"]["path"], "canonical.csv")
            self.assertEqual(files["01_RESEARCH/figures/.model_cache/cache.bin"]["classification"], "transient")
            self.assertEqual(files["01_RESEARCH/figures/.model_cache/cache.bin"]["removal_permission_gate"], "not-authorized-by-this-task")
            self.assertIn("historical_comparison", documents["current_state"])

    def test_phase32_documents_validate_and_reconcile_from_outputs_alone(self) -> None:
        """W02 findings: schema pass is durable, code-like set is enumerated, conflicts stay unresolved."""
        try:
            import jsonschema  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            self.skipTest("requires isolated requirements-py39.txt dependency")
        schema_root = pathlib.Path(__file__).resolve().parents[1] / "migration" / "schema"
        from migration.build_analytics_migration_inventory import build_phase32_documents
        with tempfile.TemporaryDirectory() as temporary:
            fixture = pathlib.Path(temporary)
            hermes = fixture / "HERMES"
            data = fixture / "1_DATA"
            outputs = fixture / "3_OUTPUTS" / "releases" / "O-20260906-01" / "artifacts"
            for path in (hermes, data, outputs):
                path.mkdir(parents=True)
            (data / "unrelated.csv").write_text("unrelated\n", encoding="utf-8")
            (hermes / "01_RESEARCH" / "data" / "graph").mkdir(parents=True)
            (hermes / "01_RESEARCH" / "data" / "graph" / "ge16-knowledge-graph.json").write_text("{}\n", encoding="utf-8")
            (hermes / "01_RESEARCH" / "figures" / "_draft" / ".venv" / "lib").mkdir(parents=True)
            (hermes / "01_RESEARCH" / "figures" / "_draft" / ".venv" / "lib" / "vendored.py").write_text("pass\n", encoding="utf-8")
            (hermes / "01_RESEARCH" / "figures" / "search_figures.py").write_text("pass\n", encoding="utf-8")
            (hermes / "01_RESEARCH" / "figures" / "ge16-parties.json").write_text("[]\n", encoding="utf-8")
            (hermes / "03_REPORTS" / "states" / "DUN Johor" / "latest").mkdir(parents=True)
            (hermes / "03_REPORTS" / "states" / "DUN Johor" / "latest" / "GE16_Johor_Report.md").write_text("report\n", encoding="utf-8")
            (hermes / "07_MANUAL").mkdir()
            (hermes / "07_MANUAL" / "GE16_System_Manual_v3.md").write_text("manual\n", encoding="utf-8")
            (hermes / ".tracking").mkdir()
            (hermes / ".tracking" / "LATEST.json").write_text("{}\n", encoding="utf-8")
            (hermes / "08_HANDOFF").mkdir()
            (hermes / "08_HANDOFF" / "phase_1_1_migration_inventory.json").write_text("{}\n", encoding="utf-8")

            documents = build_phase32_documents(hermes, data, outputs)
            validate_document(documents["runtime_dependency"], schema_root / "runtime-dependency-inventory.schema.json")
            validate_document(documents["rename_disposition"], schema_root / "rename-disposition.schema.json")
            validate_document(documents["current_state"], schema_root / "current-state.schema.json")

            disposition = documents["rename_disposition"]["files"]
            code_files = documents["runtime_dependency"]["code_files"]
            current_state = documents["current_state"]
            disposition_paths = {record["path"] for record in disposition}
            code_paths = {record["path"] for record in code_files}
            self.assertEqual(disposition_paths & code_paths, set())
            self.assertEqual(len(disposition) + len(code_files), current_state["file_count"])
            self.assertEqual(current_state["code_file_count"], len(code_files))

            by_path = {record["path"]: record for record in disposition}
            for conflicted in (
                "01_RESEARCH/data/graph/ge16-knowledge-graph.json",
                "01_RESEARCH/figures/ge16-parties.json",
                "08_HANDOFF/phase_1_1_migration_inventory.json",
            ):
                self.assertEqual(by_path[conflicted]["disposition_status"], "unresolved")
                self.assertEqual(len(by_path[conflicted]["disposition_evidence"]), 2)
                self.assertTrue(by_path[conflicted]["disposition_reason"].startswith("unresolved:"))
            self.assertEqual(by_path[".tracking/LATEST.json"]["disposition_status"], "unresolved")
            self.assertIn("3_OUTPUTS/2_ANALYTICS/tracking", by_path[".tracking/LATEST.json"]["disposition_reason"])
            for resolved, root in (("07_MANUAL/GE16_System_Manual_v3.md", "OPS/manual"),):
                self.assertEqual(by_path[resolved]["disposition_status"], "resolved")
                self.assertEqual(by_path[resolved]["destination"]["root"], root)

            vendored = {record["path"]: record for record in code_files}
            self.assertIn("01_RESEARCH/figures/_draft/.venv/lib/vendored.py", vendored)
            self.assertEqual(vendored["01_RESEARCH/figures/_draft/.venv/lib/vendored.py"]["active_code_status"], "excluded")
            for record in documents["runtime_dependency"]["latest_reports"]["records"]:
                self.assertIn("path", record)
                self.assertRegex(record["sha256"], r"^[0-9a-f]{64}$")

    @contextmanager
    def _fault_deadline(self):
        """Keep a defective fault mock from making a write loop run forever."""
        def expired(signum, frame):
            raise TimeoutError("fault-injection deadline exceeded")

        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, 5)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def _writer_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        fixture = pathlib.Path(temporary.name)
        source = fixture / "source"
        source.mkdir()
        (source / "tool.py").write_text("pass\n", encoding="utf-8")
        evidence = fixture / "OPS" / "migration" / "evidence"
        evidence.mkdir(parents=True)
        return temporary, fixture, evidence, build_documents(source)

    def test_lstat_inventory_is_sorted_and_never_dereferences_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "z.txt").write_text("z", encoding="utf-8")
            (root / "src.py").write_text("print('ok')\n", encoding="utf-8")
            os.symlink("z.txt", root / "link")

            records = inventory_tree(root)

            self.assertEqual([record["path"] for record in records], ["link", "src.py", "z.txt"])
            link = records[0]
            self.assertEqual(link["file_type"], "symlink")
            self.assertEqual(link["symlink_target"], "z.txt")
            self.assertIsNone(link["sha256"])
            self.assertEqual(records[1]["sha256"], hashlib.sha256(b"print('ok')\n").hexdigest())

    def test_exclusions_are_not_active_code_and_unresolved_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "active.py").write_text("pass\n", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("ignored", encoding="utf-8")
            (root / "cache").mkdir()
            (root / "cache" / "generated.py").write_text("pass\n", encoding="utf-8")
            (root / "bytecode.pyc").write_bytes(b"compiled")

            documents = build_documents(root)
            records = {record["path"]: record for record in documents["rename_disposition"]["files"]}

            self.assertEqual(records["active.py"]["classification"], "runtime")
            self.assertEqual(records["active.py"]["disposition_status"], "unresolved")
            self.assertEqual(records["cache/generated.py"]["active_code_status"], "excluded")
            self.assertIn("cache", records["cache/generated.py"]["classification_reason"])
            self.assertEqual(records[".git/config"]["classification"], "unresolved")
            self.assertEqual(records["bytecode.pyc"]["active_code_status"], "excluded")

    def test_duplicate_paths_are_rejected_before_document_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "file.txt").write_text("x", encoding="utf-8")
            records = inventory_tree(root)
            with self.assertRaisesRegex(InventoryError, "duplicate path"):
                build_documents(root, records=records + records)

    def test_every_record_has_required_dependency_and_git_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "tool.py").write_text("pass\n", encoding="utf-8")
            record = build_documents(root)["rename_disposition"]["files"][0]
            for field in ("inbound_callers", "reads", "writes", "destination", "counterpart"):
                self.assertIn(field, record)
            self.assertEqual(record["git"]["state"], "unresolved")
            self.assertEqual(record["dependency_status"], "unresolved")

    def test_schema_validation_never_uses_a_partial_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "tool.py").write_text("pass\n", encoding="utf-8")
            document = build_documents(root)["runtime_dependency"]
            schema = pathlib.Path(__file__).resolve().parents[1] / "migration" / "schema" / "runtime-dependency-inventory.schema.json"
            try:
                import jsonschema  # type: ignore[import-not-found,unused-ignore]
            except ImportError:
                with self.assertRaisesRegex(InventoryError, "refusing partial validation"):
                    validate_document(document, schema)
            else:
                validate_document(document, schema)

    def test_draft202012_schema_accepts_valid_document_and_rejects_malformed_fields(self) -> None:
        try:
            import jsonschema  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            self.skipTest("requires isolated requirements-py39.txt dependency")
        schema_root = pathlib.Path(__file__).resolve().parents[1] / "migration" / "schema"
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "tool.py").write_text("pass\n", encoding="utf-8")
            documents = build_documents(root)
            validate_document(documents["runtime_dependency"], schema_root / "runtime-dependency-inventory.schema.json")
            malformed_format = json.loads(json.dumps(documents["runtime_dependency"]))
            malformed_format["format"] = "wrong"
            with self.assertRaisesRegex(InventoryError, "schema validation failed"):
                validate_document(malformed_format, schema_root / "runtime-dependency-inventory.schema.json")
            malformed_path = json.loads(json.dumps(documents["rename_disposition"]))
            malformed_path["files"][0]["path"] = "../unsafe.py"
            with self.assertRaisesRegex(InventoryError, "schema validation failed"):
                validate_document(malformed_path, schema_root / "rename-disposition.schema.json")
            duplicate = json.loads(json.dumps(documents["rename_disposition"]))
            duplicate["files"].append(dict(duplicate["files"][0]))
            with self.assertRaisesRegex(InventoryError, "schema validation failed"):
                validate_document(duplicate, schema_root / "rename-disposition.schema.json")

    def test_writer_is_deterministic_preserves_source_and_rejects_unsafe_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = pathlib.Path(temporary)
            source = fixture / "source"
            source.mkdir()
            (source / "tool.py").write_text("print('fixture')\n", encoding="utf-8")
            (source / "notes.txt").write_text("unchanged\n", encoding="utf-8")
            source_before = {path.name: path.read_bytes() for path in source.iterdir()}
            evidence = fixture / "OPS" / "migration" / "evidence"
            evidence.mkdir(parents=True)
            documents = build_documents(source)
            with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence):
                first_paths = write_documents(documents, evidence)
                self.assertEqual([path.name for path in first_paths], list(GENERATED_OUTPUT_FILENAMES))
                first_bytes = {path.name: path.read_bytes() for path in first_paths}
                second_paths = write_documents(documents, evidence)
                self.assertEqual(first_bytes, {path.name: path.read_bytes() for path in second_paths})
                self.assertEqual(source_before, {path.name: path.read_bytes() for path in source.iterdir()})
                (evidence / GENERATED_OUTPUT_FILENAMES[0]).unlink()
                os.symlink(str(fixture / "outside"), evidence / GENERATED_OUTPUT_FILENAMES[0])
                with self.assertRaisesRegex(InventoryError, "non-regular"):
                    write_documents(documents, evidence)
            with self.assertRaisesRegex(InventoryError, "canonical"):
                write_documents(documents, fixture / "not-ops")

    def test_source_root_symlink_is_rejected_without_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = pathlib.Path(temporary)
            target = fixture / "target"
            target.mkdir()
            (target / "secret.py").write_text("secret", encoding="utf-8")
            link = fixture / "source-link"
            os.symlink(target, link)
            with self.assertRaisesRegex(InventoryError, "symlink"):
                inventory_tree(link)

    def test_main_rejects_missing_or_empty_source_without_writing_outputs(self) -> None:
        """Publication is fail-closed before an empty inventory can replace evidence."""
        with tempfile.TemporaryDirectory() as temporary:
            fixture = pathlib.Path(temporary)
            evidence = fixture / "OPS" / "migration" / "evidence"
            evidence.mkdir(parents=True)
            schema_root = pathlib.Path(__file__).resolve().parents[1] / "migration" / "schema"
            before = {path.name: path.read_bytes() for path in evidence.iterdir()}
            with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence):
                missing = fixture / "does-not-exist"
                with self.assertRaisesRegex(InventoryError, str(missing)):
                    main(["--source-root", str(missing), "--evidence-root", str(evidence), "--schema-root", str(schema_root)])
                empty = fixture / "empty"
                empty.mkdir()
                with self.assertRaisesRegex(InventoryError, str(empty)):
                    main(["--source-root", str(empty), "--evidence-root", str(evidence), "--schema-root", str(schema_root)])
            self.assertEqual(before, {path.name: path.read_bytes() for path in evidence.iterdir()})

    def test_symlinked_canonical_evidence_root_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = pathlib.Path(temporary)
            source = fixture / "source"
            source.mkdir()
            (source / "tool.py").write_text("pass\n", encoding="utf-8")
            evidence = fixture / "OPS" / "migration" / "evidence"
            evidence.mkdir(parents=True)
            alias = fixture / "evidence-alias"
            os.symlink(evidence, alias)
            documents = build_documents(source)
            with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence):
                first = write_documents(documents, evidence)
                expected = {path.name: path.read_bytes() for path in first}
                second = write_documents(documents, alias)
            self.assertEqual(expected, {path.name: path.read_bytes() for path in second})

    def test_semantic_validation_rejects_duplicate_code_paths_and_empty_unavailable_delta(self) -> None:
        try:
            import jsonschema  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            self.skipTest("requires isolated requirements-py39.txt dependency")
        schema_root = pathlib.Path(__file__).resolve().parents[1] / "migration" / "schema"
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "tool.py").write_text("pass\n", encoding="utf-8")
            runtime = build_documents(root)["runtime_dependency"]
            code_record = dict(runtime["files"][0])
            for field in ("classification", "disposition_status", "disposition_reason", "destination", "counterpart"):
                code_record.pop(field)
            duplicate_record = dict(code_record)
            duplicate_record["classification_reason"] = "different record, same path"
            runtime["code_files"] = [code_record, duplicate_record]
            with self.assertRaisesRegex(InventoryError, "code_files must have unique"):
                validate_document(runtime, schema_root / "runtime-dependency-inventory.schema.json")
            current = build_documents(root)["current_state"]
            current["historical_comparison"] = {"path_reconciliation": {"status": "unavailable", "added": [], "removed": None, "changed": None}}
            with self.assertRaisesRegex(InventoryError, "path_reconciliation"):
                validate_document(current, schema_root / "current-state.schema.json")

    def test_schema_rejects_coherence_errors_and_semantic_duplicate_paths(self) -> None:
        try:
            import jsonschema  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            self.skipTest("requires isolated requirements-py39.txt dependency")
        schema_root = pathlib.Path(__file__).resolve().parents[1] / "migration" / "schema"
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "tool.py").write_text("pass\n", encoding="utf-8")
            document = build_documents(root)["rename_disposition"]
            malformed = json.loads(json.dumps(document))
            malformed["files"][0].update({"sha256": None, "symlink_target": "wrong", "git": {"tracked": True, "ignored": True, "state": "unresolved"}})
            with self.assertRaisesRegex(InventoryError, "schema validation failed"):
                validate_document(malformed, schema_root / "rename-disposition.schema.json")
            duplicate = json.loads(json.dumps(document))
            altered = dict(duplicate["files"][0])
            altered["classification_reason"] = "different record, same path"
            duplicate["files"].append(altered)
            with self.assertRaisesRegex(InventoryError, "semantic validation failed"):
                validate_document(duplicate, schema_root / "rename-disposition.schema.json")
            validate_document(build_documents(root)["current_state"], schema_root / "current-state.schema.json")

    def test_hardlinks_and_publish_faults_preserve_prior_declared_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = pathlib.Path(temporary)
            source = fixture / "source"
            source.mkdir()
            (source / "tool.py").write_text("pass\n", encoding="utf-8")
            evidence = fixture / "OPS" / "migration" / "evidence"
            evidence.mkdir(parents=True)
            documents = build_documents(source)
            with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence):
                outside = fixture / "outside"
                outside.write_text("KEEP", encoding="utf-8")
                os.link(outside, evidence / GENERATED_OUTPUT_FILENAMES[0])
                with self.assertRaisesRegex(InventoryError, "linked"):
                    write_documents(documents, evidence)
                self.assertEqual(outside.read_text(encoding="utf-8"), "KEEP")
                (evidence / GENERATED_OUTPUT_FILENAMES[0]).unlink()
                before = {}
                for name in GENERATED_OUTPUT_FILENAMES:
                    path = evidence / name
                    path.write_text("old-" + name, encoding="utf-8")
                    before[name] = path.read_bytes()
                real_rename = os.rename
                calls = {"count": 0}
                def fail_second_publish(*args, **kwargs):
                    calls["count"] += 1
                    if calls["count"] == 4:
                        raise OSError("injected publish fault")
                    return real_rename(*args, **kwargs)
                with mock.patch.object(inventory_module.os, "rename", side_effect=fail_second_publish):
                    with self.assertRaisesRegex(InventoryError, "prior declared outputs restored"):
                        write_documents(documents, evidence)
                self.assertEqual(before, {name: (evidence / name).read_bytes() for name in GENERATED_OUTPUT_FILENAMES})

    def test_ancestor_substitution_fails_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = pathlib.Path(temporary)
            source = fixture / "source"
            source.mkdir()
            (source / "tool.py").write_text("pass\n", encoding="utf-8")
            migration = fixture / "OPS" / "migration"
            evidence = migration / "evidence"
            evidence.mkdir(parents=True)
            outside = fixture / "outside"
            (outside / "evidence").mkdir(parents=True)
            documents = build_documents(source)
            original_stage = inventory_module._write_staged
            swapped = {"done": False}
            def stage_then_substitute(*args, **kwargs):
                result = original_stage(*args, **kwargs)
                if not swapped["done"]:
                    migration.rename(fixture / "migration-held")
                    os.symlink(outside, migration)
                    swapped["done"] = True
                return result
            with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), mock.patch.object(inventory_module, "_write_staged", side_effect=stage_then_substitute):
                with self.assertRaisesRegex(InventoryError, "canonical evidence root changed"):
                    write_documents(documents, evidence)
            self.assertEqual(list((outside / "evidence").iterdir()), [])
            self.assertEqual(list((fixture / "migration-held" / "evidence").glob("ge16-*.json")), [])

    def test_unrelated_replacement_is_not_overwritten_during_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = pathlib.Path(temporary)
            source = fixture / "source"
            source.mkdir()
            (source / "tool.py").write_text("pass\n", encoding="utf-8")
            evidence = fixture / "OPS" / "migration" / "evidence"
            evidence.mkdir(parents=True)
            for name in GENERATED_OUTPUT_FILENAMES:
                (evidence / name).write_text("old-" + name, encoding="utf-8")
            documents = build_documents(source)
            real_rename = os.rename
            calls = {"count": 0}
            def replace_second_output(*args, **kwargs):
                result = real_rename(*args, **kwargs)
                calls["count"] += 1
                if calls["count"] == 2:
                    replacement = evidence / GENERATED_OUTPUT_FILENAMES[1]
                    replacement.unlink()
                    replacement.write_text("unrelated replacement", encoding="utf-8")
                return result
            with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), mock.patch.object(inventory_module.os, "rename", side_effect=replace_second_output):
                with self.assertRaisesRegex(InventoryError, "rollback incomplete.*prior output not restored"):
                    write_documents(documents, evidence)
            self.assertEqual((evidence / GENERATED_OUTPUT_FILENAMES[0]).read_text(encoding="utf-8"), "old-" + GENERATED_OUTPUT_FILENAMES[0])
            self.assertEqual((evidence / GENERATED_OUTPUT_FILENAMES[1]).read_text(encoding="utf-8"), "unrelated replacement")
            self.assertEqual((evidence / GENERATED_OUTPUT_FILENAMES[2]).read_text(encoding="utf-8"), "old-" + GENERATED_OUTPUT_FILENAMES[2])

    def test_staged_replacement_before_rename_is_rejected_and_preserved(self) -> None:
        for replacement_kind in ("regular", "hardlink", "symlink"):
            with self.subTest(replacement_kind=replacement_kind), tempfile.TemporaryDirectory() as temporary:
                fixture = pathlib.Path(temporary)
                source = fixture / "source"
                source.mkdir()
                (source / "tool.py").write_text("pass\n", encoding="utf-8")
                evidence = fixture / "OPS" / "migration" / "evidence"
                evidence.mkdir(parents=True)
                outside = fixture / "outside"
                outside.write_bytes(b"UNRELATED-BYTES")
                documents = build_documents(source)
                real_rename = os.rename
                replaced = {"done": False}

                def substitute_then_rename(src, dst, *args, **kwargs):
                    if not replaced["done"] and src.endswith(".tmp") and dst == GENERATED_OUTPUT_FILENAMES[0]:
                        os.unlink(src, dir_fd=kwargs["src_dir_fd"])
                        replacement = evidence / src
                        if replacement_kind == "regular":
                            replacement.write_bytes(b"UNRELATED-BYTES")
                        elif replacement_kind == "hardlink":
                            os.link(outside, replacement)
                        else:
                            os.symlink(outside, replacement)
                        replaced["done"] = True
                    return real_rename(src, dst, *args, **kwargs)

                with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), mock.patch.object(inventory_module.os, "rename", side_effect=substitute_then_rename):
                    with self.assertRaisesRegex(InventoryError, "rollback incomplete.*prior output not restored"):
                        write_documents(documents, evidence)
                published = evidence / GENERATED_OUTPUT_FILENAMES[0]
                if replacement_kind == "symlink":
                    self.assertTrue(published.is_symlink())
                else:
                    self.assertEqual(published.read_bytes(), b"UNRELATED-BYTES")
                self.assertEqual(outside.read_bytes(), b"UNRELATED-BYTES")

    def test_staging_write_and_descriptor_faults_leave_no_known_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = pathlib.Path(temporary)
            source = fixture / "source"
            source.mkdir()
            (source / "tool.py").write_text("pass\n", encoding="utf-8")
            evidence = fixture / "OPS" / "migration" / "evidence"
            evidence.mkdir(parents=True)
            documents = build_documents(source)
            real_write = os.write

            def short_write(descriptor, data):
                return real_write(descriptor, data[:11])

            with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), mock.patch.object(inventory_module.os, "write", side_effect=short_write):
                paths = write_documents(documents, evidence)
            for path in paths:
                json.loads(path.read_text(encoding="utf-8"))
            for path in paths:
                path.unlink()
            with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), mock.patch.object(inventory_module.os, "write", return_value=0):
                with self.assertRaisesRegex(InventoryError, "short write"):
                    write_documents(documents, evidence)
            self.assertEqual(list(evidence.glob(".*.tmp")), [])
            with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), mock.patch.object(inventory_module.os, "fsync", side_effect=OSError("fsync fault")):
                with self.assertRaisesRegex(OSError, "fsync fault"):
                    write_documents(documents, evidence)
            self.assertEqual(list(evidence.glob(".*.tmp")), [])
            real_open = os.open

            def fail_staged_open(name, *args, **kwargs):
                if isinstance(name, str) and name.startswith(".ge16-"):
                    raise OSError("descriptor acquisition fault")
                return real_open(name, *args, **kwargs)

            with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), mock.patch.object(inventory_module.os, "open", side_effect=fail_staged_open):
                with self.assertRaisesRegex(OSError, "descriptor acquisition fault"):
                    write_documents(documents, evidence)
            self.assertEqual(list(evidence.glob(".*.tmp")), [])

    def test_staging_close_faults_leave_no_known_temporary_for_every_output(self) -> None:
        """A deferred close error must clean the identity already captured by fstat."""
        for target_number in range(len(GENERATED_OUTPUT_FILENAMES)):
            with self.subTest(staged_output=GENERATED_OUTPUT_FILENAMES[target_number]):
                temporary, fixture, evidence, documents = self._writer_fixture()
                self.addCleanup(temporary.cleanup)
                real_open = os.open
                real_close = os.close
                staged_descriptors = []

                def remember_staged_open(name, *args, **kwargs):
                    descriptor = real_open(name, *args, **kwargs)
                    if isinstance(name, str) and name.startswith(".ge16-") and name.endswith(".tmp"):
                        staged_descriptors.append(descriptor)
                    return descriptor

                def fail_target_close(descriptor):
                    if (len(staged_descriptors) > target_number
                            and descriptor == staged_descriptors[target_number]):
                        real_close(descriptor)
                        raise OSError("deferred close fault")
                    return real_close(descriptor)

                with self._fault_deadline(), mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), \
                        mock.patch.object(inventory_module.os, "open", side_effect=remember_staged_open), \
                        mock.patch.object(inventory_module.os, "close", side_effect=fail_target_close):
                    with self.assertRaisesRegex(OSError, "deferred close fault"):
                        write_documents(documents, evidence)
                self.assertEqual(list(evidence.glob(".*.tmp")), [])
                self.assertEqual(list(evidence.glob("ge16-*.json")), [])

    def test_staged_identity_substitution_with_identical_bytes_is_rejected_in_both_windows(self) -> None:
        """Only dev/inode checks distinguish these replacements from our staged file."""
        for window in ("before-rename", "verifier-entry"):
            with self.subTest(window=window):
                temporary, fixture, evidence, documents = self._writer_fixture()
                self.addCleanup(temporary.cleanup)
                replaced = {"done": False}
                real_rename = os.rename
                original_verify = inventory_module._verify_staged_output

                def substitute(name):
                    path = evidence / name
                    contents = path.read_bytes()
                    path.unlink()
                    path.write_bytes(contents)
                    replaced["done"] = True

                def rename_after_substitution(src, dst, *args, **kwargs):
                    if (window == "before-rename" and not replaced["done"]
                            and src.endswith(".tmp") and dst == GENERATED_OUTPUT_FILENAMES[0]):
                        substitute(src)
                    return real_rename(src, dst, *args, **kwargs)

                def verify_after_substitution(directory_fd, name, expected):
                    if window == "verifier-entry" and not replaced["done"] and name.endswith(".tmp"):
                        substitute(name)
                    return original_verify(directory_fd, name, expected)

                with self._fault_deadline(), mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), \
                        mock.patch.object(inventory_module.os, "rename", side_effect=rename_after_substitution), \
                        mock.patch.object(inventory_module, "_verify_staged_output", side_effect=verify_after_substitution):
                    # write_documents wraps the verifier failure in its
                    # rollback result, so its outer error is intentionally
                    # not the verifier's exact message.
                    with self.assertRaises(InventoryError):
                        write_documents(documents, evidence)
                self.assertTrue(replaced["done"])

    def test_staged_write_post_fstat_rejects_a_lying_write(self) -> None:
        temporary, fixture, evidence, documents = self._writer_fixture()
        self.addCleanup(temporary.cleanup)
        real_write = os.write

        def write_five_but_claim_complete(descriptor, data):
            real_write(descriptor, data[:5])
            return len(data)

        with self._fault_deadline(), mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), \
                mock.patch.object(inventory_module.os, "write", side_effect=write_five_but_claim_complete):
            with self.assertRaisesRegex(InventoryError, "staged output changed while writing"):
                write_documents(documents, evidence)
        self.assertEqual(list(evidence.glob(".*.tmp")), [])

    def test_pre_rename_verification_is_performed_on_the_temporary_name(self) -> None:
        """The post-rename verifier cannot substitute for this pre-publication check."""
        temporary, fixture, evidence, documents = self._writer_fixture()
        self.addCleanup(temporary.cleanup)
        original_verify = inventory_module._verify_staged_output
        calls = []

        def record_verify(directory_fd, name, expected):
            calls.append(name)
            return original_verify(directory_fd, name, expected)

        with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), \
                mock.patch.object(inventory_module, "_verify_staged_output", side_effect=record_verify):
            write_documents(documents, evidence)
        self.assertEqual(sum(name.endswith(".tmp") for name in calls), 3)
        self.assertEqual(sum(name in GENERATED_OUTPUT_FILENAMES for name in calls), 3)

    def test_staged_content_rehash_rejects_same_inode_same_size_mutation(self) -> None:
        temporary, fixture, evidence, documents = self._writer_fixture()
        self.addCleanup(temporary.cleanup)
        real_open = os.open
        changed = {"done": False}

        def mutate_before_read(name, flags, *args, **kwargs):
            if (not changed["done"] and isinstance(name, str) and name.endswith(".tmp")
                    and flags & os.O_ACCMODE == os.O_RDONLY):
                path = evidence / name
                contents = path.read_bytes()
                path.write_bytes(b"X" + contents[1:])
                changed["done"] = True
            return real_open(name, flags, *args, **kwargs)

        with self._fault_deadline(), mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), \
                mock.patch.object(inventory_module.os, "open", side_effect=mutate_before_read):
            with self.assertRaises(InventoryError):
                write_documents(documents, evidence)
        self.assertTrue(changed["done"])

    def test_rollback_does_not_replace_a_foreign_output_when_restoration_is_impossible(self) -> None:
        temporary, fixture, evidence, documents = self._writer_fixture()
        self.addCleanup(temporary.cleanup)
        for name in GENERATED_OUTPUT_FILENAMES:
            (evidence / name).write_text("old-" + name, encoding="utf-8")
        real_rename = os.rename
        calls = {"count": 0}

        def leave_foreign_second_destination(src, dst, *args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 4:
                (evidence / GENERATED_OUTPUT_FILENAMES[1]).write_bytes(b"FOREIGN")
                raise OSError("injected publication fault")
            return real_rename(src, dst, *args, **kwargs)

        with self._fault_deadline(), mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), \
                mock.patch.object(inventory_module.os, "rename", side_effect=leave_foreign_second_destination):
            with self.assertRaisesRegex(InventoryError, "cannot restore without replacing"):
                write_documents(documents, evidence)
        self.assertEqual((evidence / GENERATED_OUTPUT_FILENAMES[1]).read_bytes(), b"FOREIGN")

    def test_remove_known_staged_requires_identity_proof_before_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = pathlib.Path(temporary)
            directory_fd = os.open(evidence, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            self.addCleanup(os.close, directory_fd)
            name = ".ge16-private.tmp"
            path = evidence / name
            path.write_bytes(b"ours")
            expected = os.stat(path)
            path.unlink()
            path.write_bytes(b"foreign")
            inventory_module._remove_known_staged(directory_fd, name, expected)
            self.assertEqual(path.read_bytes(), b"foreign")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "platform has no O_NOFOLLOW")
    def test_staged_creation_requests_o_nofollow(self) -> None:
        temporary, fixture, evidence, documents = self._writer_fixture()
        self.addCleanup(temporary.cleanup)
        real_open = os.open
        flags_seen = []

        def record_open(name, flags, *args, **kwargs):
            if (isinstance(name, str) and name.startswith(".ge16-") and name.endswith(".tmp")
                    and flags & os.O_ACCMODE == os.O_WRONLY):
                flags_seen.append(flags)
            return real_open(name, flags, *args, **kwargs)

        with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence), \
                mock.patch.object(inventory_module.os, "open", side_effect=record_open):
            write_documents(documents, evidence)
        self.assertEqual(len(flags_seen), 3)
        self.assertTrue(all(flags & os.O_NOFOLLOW for flags in flags_seen))

    def test_real_cli_fixture_round_trip_rejects_noncanonical_argument(self) -> None:
        try:
            import jsonschema  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            self.skipTest("requires isolated requirements-py39.txt dependency")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = pathlib.Path(temporary)
            source = fixture / "source"
            source.mkdir()
            (source / "tool.py").write_text("pass\n", encoding="utf-8")
            evidence = fixture / "OPS" / "migration" / "evidence"
            evidence.mkdir(parents=True)
            schema_root = pathlib.Path(__file__).resolve().parents[1] / "migration" / "schema"
            with mock.patch.object(inventory_module, "OPS_EVIDENCE_ROOT", evidence):
                arguments = ["--source-root", str(source), "--evidence-root", str(evidence), "--schema-root", str(schema_root)]
                self.assertEqual(main(arguments), 0)
                self.assertEqual(main(arguments), 0)
                first = {name: (evidence / name).read_bytes() for name in GENERATED_OUTPUT_FILENAMES}
                self.assertEqual(main(arguments), 0)
                self.assertEqual(first, {name: (evidence / name).read_bytes() for name in GENERATED_OUTPUT_FILENAMES})
                with self.assertRaisesRegex(InventoryError, "CLI output root"):
                    main(["--source-root", str(source), "--evidence-root", str(fixture / "elsewhere"), "--schema-root", str(schema_root)])

    def test_git_fixture_reports_tracked_ignored_and_untracked_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            subprocess.run(("git", "init", "-q", str(root)), check=True)
            (root / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
            (root / "tracked.py").write_text("pass\n", encoding="utf-8")
            (root / "ignored.log").write_text("ignored\n", encoding="utf-8")
            (root / "untracked.txt").write_text("new\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(root), "add", ".gitignore", "tracked.py"), check=True)
            records = {record["path"]: record for record in inventory_tree(root, None)}
            self.assertEqual(records["tracked.py"]["git"], {"tracked": None, "ignored": None, "state": "unresolved"})
            documents = build_documents(root)
            states = {record["path"]: record["git"] for record in documents["rename_disposition"]["files"]}
            self.assertEqual(states["tracked.py"], {"tracked": True, "ignored": False, "state": "tracked"})
            self.assertEqual(states["ignored.log"], {"tracked": False, "ignored": True, "state": "ignored"})
            self.assertEqual(states["untracked.txt"], {"tracked": False, "ignored": False, "state": "untracked"})
