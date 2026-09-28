import errno
import importlib
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

try:
    provenance_tests = importlib.import_module("test_canonical_data_provenance")
    extraction_tests = importlib.import_module("test_extract_canonical_data")
except ModuleNotFoundError:
    provenance_tests = importlib.import_module("tests.test_canonical_data_provenance")
    extraction_tests = importlib.import_module("tests.test_extract_canonical_data")


class PersistentPathReplacementTests(unittest.TestCase):
    def extraction_fixture(self):
        fixture = extraction_tests.CanonicalDataExtractionTests(
            "test_fresh_explicit_import_copies_bytes_and_validates_without_legacy_root"
        )
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def validation_fixture(self):
        fixture = provenance_tests.CanonicalDataProvenanceTests(
            "test_accepts_matching_manifest_and_trees"
        )
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_source_scan_rejects_root_replaced_during_enumeration(self):
        """Enumeration must not silently switch to a replacement source root."""
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        source_root = fixture.workspace / "scan-source"
        source_root.mkdir()
        (source_root / "shared.bin").write_bytes(b"original\n")
        relocated = fixture.workspace / "scan-source-original"
        replacement = fixture.workspace / "scan-source-replacement"
        selected = source_root.absolute()
        source_stat = source_root.stat()
        extractor._ACTIVE_DIRECTORY_IDENTITIES = {
            selected: (source_stat.st_dev, source_stat.st_ino)
        }
        extractor._ACTIVE_DIRECTORY_DESCRIPTORS = None
        self.addCleanup(setattr, extractor, "_ACTIVE_DIRECTORY_IDENTITIES", None)
        switched = False

        def walk_with_replacement(root, *, topdown, onerror=None, followlinks):
            nonlocal switched
            yield os.fspath(root), [], ["shared.bin"]
            source_root.rename(relocated)
            replacement.mkdir()
            (replacement / "shared.bin").write_bytes(b"replacement\n")
            replacement.rename(source_root)
            switched = True
            yield os.fspath(root), [], ["shared.bin"]

        with mock.patch.object(extractor.os, "walk", side_effect=walk_with_replacement):
            with self.assertRaisesRegex(RuntimeError, "source (root|directory).*changed identity"):
                extractor._source_files(source_root)

        self.assertTrue(switched)

    def test_extractor_rejects_nested_directory_replaced_after_enumeration(self):
        """A directory selected by os.walk must retain its identity until copied."""
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        source_root = fixture.workspace / "nested-source"
        nested = source_root / "nested"
        nested.mkdir(parents=True)
        (nested / "payload.bin").write_bytes(b"original\n")
        relocated = fixture.workspace / "nested-source-original"
        replacement = fixture.workspace / "nested-source-replacement"
        root_stat = source_root.stat()
        extractor._ACTIVE_DIRECTORY_IDENTITIES = {
            source_root.absolute(): (root_stat.st_dev, root_stat.st_ino)
        }
        extractor._ACTIVE_DIRECTORY_DESCRIPTORS = None
        self.addCleanup(setattr, extractor, "_ACTIVE_DIRECTORY_IDENTITIES", None)

        def walk_with_replacement(root, *, topdown, onerror=None, followlinks=False):
            yield os.fspath(root), ["nested"], []
            nested.rename(relocated)
            replacement.mkdir()
            (replacement / "payload.bin").write_bytes(b"REPLACEMENT\n")
            replacement.rename(nested)
            yield os.fspath(nested), [], ["payload.bin"]

        with mock.patch.object(extractor.os, "walk", side_effect=walk_with_replacement):
            with self.assertRaisesRegex(RuntimeError, "source directory.*changed identity"):
                extractor._source_files(source_root)

    def test_validator_rejects_nested_directory_replaced_after_enumeration(self):
        """Validator scans must not accept replacement bytes from nested directories."""
        fixture = self.validation_fixture()
        validator = provenance_tests.load_validator_module()
        root = Path(fixture.tempdir.name) / "nested-validation"
        nested = root / "nested"
        nested.mkdir(parents=True)
        (nested / "payload.bin").write_bytes(b"original\n")
        relocated = Path(fixture.tempdir.name) / "nested-validation-original"
        replacement = Path(fixture.tempdir.name) / "nested-validation-replacement"
        root_stat = root.stat()
        validator._ACTIVE_DIRECTORY_IDENTITIES = {
            root.absolute(): (root_stat.st_dev, root_stat.st_ino)
        }
        validator._ACTIVE_DIRECTORY_DESCRIPTORS = None
        self.addCleanup(setattr, validator, "_ACTIVE_DIRECTORY_IDENTITIES", None)
        self.addCleanup(setattr, validator, "_ACTIVE_DIRECTORY_DESCRIPTORS", None)

        def walk_with_replacement(scan_root, *, topdown, followlinks=False, onerror=None):
            yield os.fspath(scan_root), ["nested"], []
            nested.rename(relocated)
            replacement.mkdir()
            (replacement / "payload.bin").write_bytes(b"REPLACEMENT\n")
            replacement.rename(nested)
            yield os.fspath(nested), [], ["payload.bin"]

        with mock.patch.object(validator.os, "walk", side_effect=walk_with_replacement):
            with self.assertRaisesRegex(
                validator.ValidationError, "destination tree.*changed identity"
            ):
                validator.files_under(root, "destination tree")

    def test_anchored_traversal_preserves_primary_open_failure_when_close_fails(self):
        """A close failure must not hide a failed later anchored open."""
        fixture = self.extraction_fixture()
        source_root = fixture.workspace / "anchor-root"
        (source_root / "child").mkdir(parents=True)

        for loader in (
            extraction_tests.load_extractor_module,
            provenance_tests.load_validator_module,
        ):
            with self.subTest(loader=loader.__name__):
                module = loader()
                error_type = getattr(module, "ValidationError", RuntimeError)
                root_descriptor = os.open(
                    source_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                child_descriptor = None
                source_stat = source_root.stat()
                module._ACTIVE_DIRECTORY_IDENTITIES = {
                    source_root.absolute(): (source_stat.st_dev, source_stat.st_ino)
                }
                module._ACTIVE_DIRECTORY_DESCRIPTORS = {
                    source_root.absolute(): root_descriptor
                }
                real_open = module.os.open
                real_close = module.os.close

                def fail_later_open(path, flags, *args, **kwargs):
                    nonlocal child_descriptor
                    if path == "missing":
                        raise OSError(errno.EACCES, "injected primary open failure")
                    descriptor = real_open(path, flags, *args, **kwargs)
                    if path == "child":
                        child_descriptor = descriptor
                    return descriptor

                def fail_child_close(descriptor):
                    if descriptor == child_descriptor:
                        raise OSError(errno.EIO, "injected close failure")
                    return real_close(descriptor)

                try:
                    with mock.patch.object(module.os, "open", side_effect=fail_later_open), mock.patch.object(
                        module.os, "close", side_effect=fail_child_close
                    ):
                        with self.assertRaisesRegex(error_type, "cannot open .*missing"):
                            module._open_anchored_directory(source_root / "child" / "missing", "anchor")
                finally:
                    module._ACTIVE_DIRECTORY_IDENTITIES = None
                    module._ACTIVE_DIRECTORY_DESCRIPTORS = None
                    if child_descriptor is not None:
                        try:
                            real_close(child_descriptor)
                        except OSError:
                            pass
                    real_close(root_descriptor)

    def test_destination_directory_close_failure_does_not_leak_child_descriptor(self):
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        root_descriptor = os.open(
            fixture.destination, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        root_stat = fixture.destination.stat()
        extractor._ACTIVE_DIRECTORY_IDENTITIES = {
            fixture.destination.absolute(): (root_stat.st_dev, root_stat.st_ino)
        }
        extractor._ACTIVE_DIRECTORY_DESCRIPTORS = {
            fixture.destination.absolute(): root_descriptor
        }
        real_open = extractor.os.open
        real_close = extractor.os.close
        child_descriptor = None
        failed = False

        def capture_child_open(path, flags, *args, **kwargs):
            nonlocal child_descriptor
            descriptor = real_open(path, flags, *args, **kwargs)
            if path == "child":
                child_descriptor = descriptor
            return descriptor

        def fail_first_parent_close(descriptor):
            nonlocal failed
            if descriptor != root_descriptor and child_descriptor is not None and not failed:
                failed = True
                raise OSError(errno.EIO, "injected parent close failure")
            return real_close(descriptor)

        try:
            with mock.patch.object(extractor.os, "open", side_effect=capture_child_open), mock.patch.object(
                extractor.os, "close", side_effect=fail_first_parent_close
            ):
                with self.assertRaisesRegex(RuntimeError, "cannot close destination directory"):
                    extractor._create_destination_directory(fixture.destination, extractor.PurePosixPath("child"))
            self.assertTrue(failed)
            self.assertIsNotNone(child_descriptor)
            with self.assertRaises(OSError):
                os.fstat(child_descriptor)
        finally:
            extractor._ACTIVE_DIRECTORY_IDENTITIES = None
            extractor._ACTIVE_DIRECTORY_DESCRIPTORS = None
            try:
                real_close(root_descriptor)
            except OSError:
                pass

    def test_extractor_rejects_source_ancestor_replaced_after_preflight(self):
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        source_ancestor = fixture.import_root / "01_RESEARCH/data"
        relocated = fixture.workspace / "relocated-source-data"
        real_open_source = extractor._open_source
        swapped = False

        def open_after_swap(path, label):
            nonlocal swapped
            if not swapped:
                source_ancestor.rename(relocated)
                os.symlink(relocated, source_ancestor)
                swapped = True
            return real_open_source(path, label)

        with mock.patch.object(extractor, "_open_source", side_effect=open_after_swap):
            with self.assertRaisesRegex(RuntimeError, "source.*(ancestor|changed|symlink)"):
                extractor.extract_canonical_data(fixture.destination, fixture.import_root)

        self.assertTrue(swapped)
        self.assertFalse((fixture.destination / "canonical-data-provenance.json").exists())

    def test_extractor_never_writes_through_replaced_destination_root(self):
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        selected_root = fixture.destination / "research/raw"
        relocated = fixture.workspace / "relocated-destination-raw"
        real_copy = extractor._copy_regular_file
        swapped = False
        redirected_write_observed = False

        def copy_after_swap(source, destination, label):
            nonlocal swapped, redirected_write_observed
            if not swapped:
                selected_root.rename(relocated)
                os.symlink(relocated, selected_root)
                swapped = True
            result = real_copy(source, destination, label)
            redirected_write_observed = any(path.is_file() for path in relocated.rglob("*"))
            return result

        with mock.patch.object(extractor, "_copy_regular_file", side_effect=copy_after_swap):
            with self.assertRaisesRegex(RuntimeError, "destination.*(ancestor|changed|symlink)"):
                extractor.extract_canonical_data(fixture.destination, fixture.import_root)

        self.assertTrue(swapped)
        self.assertFalse(redirected_write_observed)
        self.assertFalse(relocated.exists())
        self.assertFalse((fixture.destination / "canonical-data-provenance.json").exists())

    def test_extractor_does_not_publish_manifest_through_replaced_repository_root(self):
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        repository_root = fixture.destination
        relocated = fixture.workspace / "relocated-DATA"
        real_write_manifest = extractor._write_exclusive_file
        swapped = False

        def write_after_swap(path, content, label):
            nonlocal swapped
            if label == "destination manifest" and not swapped:
                repository_root.rename(relocated)
                os.symlink(relocated, repository_root)
                swapped = True
            return real_write_manifest(path, content, label)

        with mock.patch.object(
            extractor, "_write_exclusive_file", side_effect=write_after_swap
        ):
            with self.assertRaisesRegex(
                RuntimeError, "destination.*(root|ancestor|changed|symlink)"
            ):
                extractor.extract_canonical_data(repository_root, fixture.import_root)

        self.assertTrue(swapped)
        self.assertFalse((relocated / "canonical-data-provenance.json").exists())
        self.assertEqual(list(relocated.iterdir()), [])

    def test_validator_rejects_byte_identical_real_directory_replacement(self):
        fixture = self.validation_fixture()
        validator = provenance_tests.load_validator_module()
        selected_ancestor = fixture.base / "research"
        replacement = fixture.base.parent / "replacement-research"
        relocated = fixture.base.parent / "original-research"
        shutil.copytree(selected_ancestor, replacement)
        real_fingerprint = validator.fingerprint_file
        swapped = False

        def fingerprint_after_swap(path, label="validated file"):
            nonlocal swapped
            if not swapped:
                selected_ancestor.rename(relocated)
                replacement.rename(selected_ancestor)
                swapped = True
            return real_fingerprint(path, label)

        with mock.patch.object(
            validator, "fingerprint_file", side_effect=fingerprint_after_swap
        ):
            with self.assertRaisesRegex(
                validator.ValidationError, "destination.*(ancestor|changed|identity)"
            ):
                validator.validate(fixture.manifest)

        self.assertTrue(swapped)

    def test_validator_rejects_manifest_ancestor_replaced_before_read(self):
        fixture = self.validation_fixture()
        validator = provenance_tests.load_validator_module()
        container = fixture.base.parent / "container"
        container.mkdir()
        repository = container / "1_DATA"
        fixture.base.rename(repository)
        manifest = repository / fixture.manifest.name
        relocated = fixture.base.parent / "relocated-container"
        real_read = validator.read_stable_file
        swapped = False

        def read_after_swap(path, label, capture_bytes=False):
            nonlocal swapped
            if label == "manifest" and not swapped:
                container.rename(relocated)
                os.symlink(relocated, container)
                swapped = True
            return real_read(path, label, capture_bytes)

        with mock.patch.object(validator, "read_stable_file", side_effect=read_after_swap):
            with self.assertRaisesRegex(
                validator.ValidationError, "manifest.*(ancestor|parent|changed|symlink)"
            ):
                validator.validate(manifest)

        self.assertTrue(swapped)

    def test_extractor_does_not_read_source_redirected_at_open_boundary(self):
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        source_ancestor = fixture.import_root / "01_RESEARCH/data"
        source_file = sorted(
            path for path in (source_ancestor / "raw").rglob("*") if path.is_file()
        )[0]
        relative_source = source_file.relative_to(source_ancestor)
        relocated = fixture.workspace / "original-source-data"
        replacement = fixture.workspace / "replacement-source-data"
        real_open = extractor.os.open
        real_read = extractor.os.read
        redirected_descriptors = set()
        redirected_read = False
        swapped = False

        def open_after_check(path, flags, *args, **kwargs):
            nonlocal swapped
            is_file_open = not (flags & getattr(os, "O_DIRECTORY", 0))
            is_target = os.fspath(path) in {os.fspath(source_file), source_file.name}
            if is_file_open and is_target and not swapped:
                source_ancestor.rename(relocated)
                shutil.copytree(relocated, replacement)
                (replacement / relative_source).write_bytes(b"redirected source bytes\n")
                os.symlink(replacement, source_ancestor)
                swapped = True
            descriptor = real_open(path, flags, *args, **kwargs)
            if swapped and is_file_open and is_target:
                replacement_stat = (replacement / relative_source).stat()
                opened_stat = os.fstat(descriptor)
                if (opened_stat.st_dev, opened_stat.st_ino) == (
                    replacement_stat.st_dev,
                    replacement_stat.st_ino,
                ):
                    redirected_descriptors.add(descriptor)
            return descriptor

        def record_read(descriptor, size):
            nonlocal redirected_read
            if descriptor in redirected_descriptors:
                redirected_read = True
            return real_read(descriptor, size)

        with mock.patch.object(extractor.os, "open", side_effect=open_after_check), mock.patch.object(
            extractor.os, "read", side_effect=record_read
        ):
            with self.assertRaises(RuntimeError):
                extractor.extract_canonical_data(fixture.destination, fixture.import_root)

        self.assertTrue(swapped)
        self.assertFalse(redirected_read)
        self.assertEqual(list(fixture.destination.iterdir()), [])

    def test_extractor_does_not_write_destination_redirected_at_open_boundary(self):
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        destination_parent = fixture.destination / "research/raw"
        relocated = fixture.workspace / "original-destination-raw"
        redirect = fixture.workspace / "redirect-destination-raw"
        redirect.mkdir()
        real_open = extractor.os.open
        real_write = extractor.os.write
        redirected_descriptors = set()
        redirected_write = False
        swapped = False

        def open_after_check(path, flags, *args, **kwargs):
            nonlocal swapped
            is_create = bool(flags & os.O_CREAT)
            if is_create and not swapped and os.fspath(path) != "canonical-data-provenance.json":
                destination_parent.rename(relocated)
                os.symlink(redirect, destination_parent)
                swapped = True
            descriptor = real_open(path, flags, *args, **kwargs)
            if swapped and is_create:
                leaf = os.path.basename(os.fspath(path))
                redirected_path = redirect / leaf
                if redirected_path.exists():
                    redirected_stat = redirected_path.stat()
                    opened_stat = os.fstat(descriptor)
                    if (opened_stat.st_dev, opened_stat.st_ino) == (
                        redirected_stat.st_dev,
                        redirected_stat.st_ino,
                    ):
                        redirected_descriptors.add(descriptor)
            return descriptor

        def record_write(descriptor, content):
            nonlocal redirected_write
            if descriptor in redirected_descriptors:
                redirected_write = True
            return real_write(descriptor, content)

        with mock.patch.object(extractor.os, "open", side_effect=open_after_check), mock.patch.object(
            extractor.os, "write", side_effect=record_write
        ):
            with self.assertRaises(RuntimeError):
                extractor.extract_canonical_data(fixture.destination, fixture.import_root)

        self.assertTrue(swapped)
        self.assertFalse(redirected_write)
        self.assertFalse(relocated.exists())

    def test_extractor_does_not_publish_manifest_redirected_at_open_boundary(self):
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        repository_root = fixture.destination
        relocated = fixture.workspace / "original-DATA"
        redirect = fixture.workspace / "redirect-DATA"
        redirect.mkdir()
        real_open = extractor.os.open
        real_write = extractor.os.write
        redirected_descriptors = set()
        redirected_write = False
        swapped = False

        def open_after_check(path, flags, *args, **kwargs):
            nonlocal swapped
            is_manifest_create = bool(flags & os.O_CREAT) and os.path.basename(
                os.fspath(path)
            ) == "canonical-data-provenance.json"
            if is_manifest_create and not swapped:
                repository_root.rename(relocated)
                os.symlink(redirect, repository_root)
                swapped = True
            descriptor = real_open(path, flags, *args, **kwargs)
            if is_manifest_create and (redirect / "canonical-data-provenance.json").exists():
                redirected_stat = (redirect / "canonical-data-provenance.json").stat()
                opened_stat = os.fstat(descriptor)
                if (opened_stat.st_dev, opened_stat.st_ino) == (
                    redirected_stat.st_dev,
                    redirected_stat.st_ino,
                ):
                    redirected_descriptors.add(descriptor)
            return descriptor

        def record_write(descriptor, content):
            nonlocal redirected_write
            if descriptor in redirected_descriptors:
                redirected_write = True
            return real_write(descriptor, content)

        with mock.patch.object(extractor.os, "open", side_effect=open_after_check), mock.patch.object(
            extractor.os, "write", side_effect=record_write
        ):
            with self.assertRaises(RuntimeError):
                extractor.extract_canonical_data(repository_root, fixture.import_root)

        self.assertTrue(swapped)
        self.assertFalse(redirected_write)
        self.assertEqual(list(relocated.iterdir()), [])
        self.assertEqual(list(redirect.iterdir()), [])

    def test_validator_does_not_read_manifest_redirected_at_open_boundary(self):
        fixture = self.validation_fixture()
        validator = provenance_tests.load_validator_module()
        repository_root = fixture.base
        manifest = fixture.manifest
        relocated = fixture.base.parent / "original-DATA"
        replacement = fixture.base.parent / "replacement-DATA"
        shutil.copytree(repository_root, replacement)
        real_open = validator.os.open
        real_read = validator.os.read
        redirected_descriptors = set()
        redirected_read = False
        swapped = False

        def open_after_check(path, flags, *args, **kwargs):
            nonlocal swapped
            is_file_open = not (flags & getattr(os, "O_DIRECTORY", 0))
            is_manifest = os.path.basename(os.fspath(path)) == manifest.name
            if is_file_open and is_manifest and not swapped:
                repository_root.rename(relocated)
                os.symlink(replacement, repository_root)
                swapped = True
            descriptor = real_open(path, flags, *args, **kwargs)
            if swapped and is_file_open and is_manifest:
                replacement_stat = (replacement / manifest.name).stat()
                opened_stat = os.fstat(descriptor)
                if (opened_stat.st_dev, opened_stat.st_ino) == (
                    replacement_stat.st_dev,
                    replacement_stat.st_ino,
                ):
                    redirected_descriptors.add(descriptor)
            return descriptor

        def record_read(descriptor, size):
            nonlocal redirected_read
            if descriptor in redirected_descriptors:
                redirected_read = True
            return real_read(descriptor, size)

        with mock.patch.object(validator.os, "open", side_effect=open_after_check), mock.patch.object(
            validator.os, "read", side_effect=record_read
        ):
            with self.assertRaises(validator.ValidationError):
                validator.validate(manifest)

        self.assertTrue(swapped)
        self.assertFalse(redirected_read)

    def test_validator_does_not_open_manifest_through_direct_parent_symlink(self):
        fixture = self.validation_fixture()
        validator = provenance_tests.load_validator_module()
        symlinked_parent = fixture.base
        real_parent = fixture.base.parent / "real-DATA"
        symlinked_parent.rename(real_parent)
        os.symlink(real_parent, symlinked_parent)
        manifest = symlinked_parent / fixture.manifest.name
        real_open = validator.os.open
        manifest_opened = False

        def record_open(path, *args, **kwargs):
            nonlocal manifest_opened
            if os.fspath(path) == os.fspath(manifest):
                manifest_opened = True
            return real_open(path, *args, **kwargs)

        with mock.patch.object(validator.os, "open", side_effect=record_open):
            with self.assertRaises(validator.ValidationError):
                validator.validate(manifest)

        self.assertFalse(manifest_opened)

    def test_extractor_removes_owned_directory_renamed_without_replacement(self):
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        selected_directory = fixture.destination / "research/raw"
        relocated = fixture.workspace / "relocated-owned-raw"
        real_copy = extractor._copy_regular_file
        injected = False

        def fail_after_relocation(source, destination, label):
            nonlocal injected
            if not injected:
                selected_directory.rename(relocated)
                injected = True
                raise RuntimeError("injected failure after owned-directory relocation")
            return real_copy(source, destination, label)

        with mock.patch.object(extractor, "_copy_regular_file", side_effect=fail_after_relocation):
            with self.assertRaisesRegex(RuntimeError, "owned-directory relocation"):
                extractor.extract_canonical_data(fixture.destination, fixture.import_root)

        self.assertTrue(injected)
        self.assertFalse(relocated.exists())
        self.assertEqual(list(fixture.destination.iterdir()), [])

    def test_extractor_preserves_replacement_and_removes_relocated_file_after_early_fstat_failure(self):
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        real_fstat = extractor.os.fstat
        real_record = extractor._record_created_path
        created_path = None
        relocated_owned = None
        replacement_bytes = b"replacement must survive\n"
        injected = False

        def record_creation(kind, path, file_stat=None):
            nonlocal created_path
            if kind == "file" and file_stat is None and created_path is None:
                created_path = path
            return real_record(kind, path, file_stat)

        def replace_then_fail(descriptor):
            nonlocal injected, relocated_owned
            if created_path is not None and not injected:
                relocated_owned = created_path.with_name(created_path.name + ".relocated-owned")
                created_path.rename(relocated_owned)
                created_path.write_bytes(replacement_bytes)
                injected = True
                raise OSError(errno.EIO, "injected destination fstat failure after replacement")
            return real_fstat(descriptor)

        with mock.patch.object(
            extractor, "_record_created_path", side_effect=record_creation
        ), mock.patch.object(extractor.os, "fstat", side_effect=replace_then_fail):
            with self.assertRaises(RuntimeError):
                extractor.extract_canonical_data(fixture.destination, fixture.import_root)

        self.assertTrue(injected)
        self.assertIsNotNone(created_path)
        self.assertTrue(created_path.exists())
        self.assertEqual(created_path.read_bytes(), replacement_bytes)
        self.assertIsNotNone(relocated_owned)
        self.assertFalse(relocated_owned.exists())

    def test_extractor_closes_child_descriptor_when_directory_verification_fails(self):
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        real_verify = extractor._verify_directory_descriptor
        leaked_candidate = None
        injected = False

        def fail_child_verification(descriptor, path, label):
            nonlocal leaked_candidate, injected
            if not injected and path != fixture.destination and path != fixture.import_root:
                leaked_candidate = descriptor
                injected = True
                raise RuntimeError("injected directory verification failure")
            return real_verify(descriptor, path, label)

        with mock.patch.object(
            extractor, "_verify_directory_descriptor", side_effect=fail_child_verification
        ):
            with self.assertRaisesRegex(RuntimeError, "directory verification failure"):
                extractor.extract_canonical_data(fixture.destination, fixture.import_root)

        self.assertTrue(injected)
        self.assertIsNotNone(leaked_candidate)
        with self.assertRaises(OSError):
            os.fstat(leaked_candidate)

    def test_validator_closes_parent_descriptor_when_final_open_fails(self):
        fixture = self.validation_fixture()
        validator = provenance_tests.load_validator_module()
        missing_manifest = fixture.base / "missing-manifest.json"
        real_open_parent = validator._open_anchored_directory
        parent_descriptor = None

        def capture_parent(path, label):
            nonlocal parent_descriptor
            parent_descriptor = real_open_parent(path, label)
            return parent_descriptor

        with mock.patch.object(
            validator, "_open_anchored_directory", side_effect=capture_parent
        ):
            with self.assertRaises(validator.ValidationError):
                validator.validate(missing_manifest)

        self.assertIsNotNone(parent_descriptor)
        with self.assertRaises(OSError):
            os.fstat(parent_descriptor)

    def test_extractor_reports_parent_close_failure_and_closes_open_source(self):
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        source = sorted(
            path
            for path in (fixture.import_root / "01_RESEARCH/data/raw").rglob("*")
            if path.is_file()
        )[0]
        root_descriptor = os.open(
            fixture.import_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        self.addCleanup(lambda: os.close(root_descriptor))
        extractor._ACTIVE_DIRECTORY_IDENTITIES = {
            fixture.import_root.absolute(): (
                os.fstat(root_descriptor).st_dev,
                os.fstat(root_descriptor).st_ino,
            )
        }
        extractor._ACTIVE_DIRECTORY_DESCRIPTORS = {
            fixture.import_root.absolute(): root_descriptor
        }
        self.addCleanup(setattr, extractor, "_ACTIVE_DIRECTORY_IDENTITIES", None)
        self.addCleanup(setattr, extractor, "_ACTIVE_DIRECTORY_DESCRIPTORS", None)
        real_close = extractor.os.close
        real_open = extractor.os.open
        parent_descriptor = None
        source_descriptor = None
        injected = False

        def record_open(path, flags, *args, **kwargs):
            nonlocal source_descriptor
            descriptor = real_open(path, flags, *args, **kwargs)
            if os.path.basename(os.fspath(path)) == source.name and not (
                flags & getattr(os, "O_DIRECTORY", 0)
            ):
                source_descriptor = descriptor
            return descriptor

        real_open_parent = extractor._open_anchored_directory

        def capture_parent(path, label):
            nonlocal parent_descriptor
            parent_descriptor = real_open_parent(path, label)
            return parent_descriptor

        def fail_parent_close_once(descriptor):
            nonlocal injected
            if descriptor == parent_descriptor and not injected:
                injected = True
                raise OSError(errno.EIO, "injected parent close failure")
            return real_close(descriptor)

        with mock.patch.object(extractor.os, "open", side_effect=record_open), mock.patch.object(
            extractor, "_open_anchored_directory", side_effect=capture_parent
        ), mock.patch.object(extractor.os, "close", side_effect=fail_parent_close_once):
            with self.assertRaisesRegex(RuntimeError, "cannot close source ancestor"):
                extractor._open_source(source, "historic source file")

        self.assertTrue(injected)
        self.assertIsNotNone(source_descriptor)
        with self.assertRaises(OSError):
            os.fstat(source_descriptor)
        if parent_descriptor is not None:
            try:
                real_close(parent_descriptor)
            except OSError:
                pass

    def test_extractor_rolls_back_file_created_before_destination_fstat_failure(self):
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        real_fstat = extractor.os.fstat
        real_record = extractor._record_created_path
        destination_created = False
        injected = False

        def record_creation(kind, path, file_stat=None):
            nonlocal destination_created
            if kind == "file" and file_stat is None:
                destination_created = True
            return real_record(kind, path, file_stat)

        def fail_first_destination_fstat(descriptor):
            nonlocal injected
            if destination_created and not injected:
                injected = True
                raise OSError(errno.EIO, "simulated destination fstat failure")
            return real_fstat(descriptor)

        with mock.patch.object(
            extractor, "_record_created_path", side_effect=record_creation
        ), mock.patch.object(extractor.os, "fstat", side_effect=fail_first_destination_fstat):
            with self.assertRaisesRegex(RuntimeError, "cannot copy historic source file"):
                extractor.extract_canonical_data(fixture.destination, fixture.import_root)

        self.assertTrue(injected)
        self.assertEqual(list(fixture.destination.iterdir()), [])

    def test_extractor_preserves_directory_replacement_after_verification_failure(self):
        """Rollback must not rmdir a replacement when creation identity is unknown."""
        fixture = self.extraction_fixture()
        extractor = extraction_tests.load_extractor_module()
        real_fstat = extractor.os.fstat
        real_record = extractor._record_created_path
        created_directory = None
        relocated_owned = None
        replacement_created = False
        injected = False

        def record_creation(kind, path, file_stat=None):
            nonlocal created_directory
            if kind == "directory" and file_stat is None and created_directory is None:
                created_directory = path
            return real_record(kind, path, file_stat)

        def replace_then_fail(descriptor):
            nonlocal injected, relocated_owned, replacement_created
            if created_directory is not None and not injected:
                relocated_owned = created_directory.with_name(
                    created_directory.name + ".relocated-owned"
                )
                created_directory.rename(relocated_owned)
                created_directory.mkdir()
                replacement_created = True
                injected = True
                raise OSError(errno.EIO, "injected directory verification failure")
            return real_fstat(descriptor)

        with mock.patch.object(
            extractor, "_record_created_path", side_effect=record_creation
        ), mock.patch.object(extractor.os, "fstat", side_effect=replace_then_fail):
            with self.assertRaisesRegex(RuntimeError, "cannot inspect destination ancestor"):
                extractor.extract_canonical_data(fixture.destination, fixture.import_root)

        self.assertTrue(injected)
        self.assertTrue(replacement_created)
        self.assertIsNotNone(created_directory)
        self.assertTrue(created_directory.is_dir())
        self.assertIsNotNone(relocated_owned)
        # No device/inode was established before fstat failed, so cleanup cannot
        # safely identify or remove the relocated owned directory either.
        self.assertTrue(relocated_owned.is_dir())


if __name__ == "__main__":
    unittest.main()
