"""Synthetic transport tests, never evidence of native CEF execution."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "vcpkg/integration"))
import sdk_import as mod
spec = importlib.util.spec_from_file_location("package_fixtures", ROOT / "vcpkg/static/tests/test_sdk_package.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cef import ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.receipt = fixtures.synthetic_receipt("x64-linux")
        (self.source / "static-sdk-receipt.json").write_bytes(mod.sdk.json_bytes(self.receipt))
        self.prefix = self.source / "installed/x64-linux"
        for name, content in {
            "include/cef-static/include/capi/cef_app_capi.h": b"/* synthetic header */",
            "share/cef-static/cef-static-config.cmake": b"# synthetic CMake fixture\n",
            "share/cef-static/copyright": b"Synthetic license",
            "share/cef-static/static-link-inventory.json": b"{}",
            "share/cef-static/resources/icudtl.dat": b"synthetic ICU fixture",
            "lib/cef-static/cef_objects.a": b"!<arch>\n",
        }.items():
            p = self.prefix / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        (self.source / "scripts").mkdir()
        (self.source / "scripts/unwanted.cmake").write_text("# must not be imported\n")
        (self.source / "installed/vcpkg").mkdir()
        (self.source / "installed/vcpkg/status").write_text("old status database")
        self.destination = self.root / "installed target"
        self.triplet = "x64-linux-static-release"

    def bundle(self, *, limit=1000000):
        directory = self.root / "download"
        manifest = mod.sdk.package_sdk(self.source, directory, "fixture-sdk", part_bytes=limit)
        lock = json.loads((ROOT / "vcpkg/integration/release.lock.json").read_text())
        lock["tested_commit"] = fixtures.COMMIT
        lock["platforms"][self.triplet]["manifest"] = mod.sdk.file_record(manifest)
        return lock, directory

    def test_single_part_import_only_owns_cef_subtree(self):
        lock, directory = self.bundle()
        result = mod.install_bundle(lock, self.triplet, directory, self.destination)
        self.assertTrue((self.destination / "include/cef-static/include/capi/cef_app_capi.h").is_file())
        self.assertFalse((self.destination / "scripts").exists())
        self.assertFalse((self.destination / "installed").exists())
        self.assertTrue(result["consumer_requalification_required"])
        self.assertFalse(result["system_libraries_static"])
        self.assertEqual(result["source_triplet"], "x64-linux")
        self.assertEqual(result["target_triplet"], self.triplet)

    def test_multipart_import(self):
        lock, directory = self.bundle(limit=256)
        self.assertGreater(len(list(directory.glob("*.part*"))), 1)
        mod.install_bundle(lock, self.triplet, directory, self.destination)
        self.assertTrue((self.destination / "share/cef-static/acquisition.json").is_file())

    def test_linux_strict_profile_is_not_fabricated(self):
        lock, directory = self.bundle()
        for profile in ("static-third-party", "fully-static", "unexpected"):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                mod.install_bundle(
                    lock, self.triplet, directory, self.destination,
                    required_profile=profile)
        self.assertFalse(self.destination.exists())

    def test_windows_release_bytes_require_new_strict_requalification(self):
        old = self.prefix
        self.triplet = "x64-windows-static-release"
        self.prefix = self.source / "installed/x64-windows-static"
        old.rename(self.prefix)
        self.receipt = fixtures.synthetic_receipt("x64-windows-static")
        (self.source / "static-sdk-receipt.json").write_bytes(
            mod.sdk.json_bytes(self.receipt))
        lock, directory = self.bundle()
        result = mod.install_bundle(
            lock, self.triplet, directory, self.destination,
            required_profile="static-third-party")
        self.assertEqual(result["requested_profile"], "static-third-party")
        self.assertTrue(result["strict_requalification_required"])
        self.assertTrue(result["consumer_requalification_required"])
        self.assertNotIn("third_party_libraries_static", result)

    def test_windows_release_bytes_still_cannot_claim_fully_static(self):
        old = self.prefix
        self.triplet = "x64-windows-static-release"
        self.prefix = self.source / "installed/x64-windows-static"
        old.rename(self.prefix)
        self.receipt = fixtures.synthetic_receipt("x64-windows-static")
        (self.source / "static-sdk-receipt.json").write_bytes(
            mod.sdk.json_bytes(self.receipt))
        lock, directory = self.bundle()
        with self.assertRaises(ValueError):
            mod.install_bundle(
                lock, self.triplet, directory, self.destination,
                required_profile="fully-static")

    def test_source_receipt_is_preserved(self):
        lock, directory = self.bundle()
        mod.install_bundle(lock, self.triplet, directory, self.destination)
        receipt = json.loads((self.destination / "share/cef-static/upstream-sdk-receipt.json").read_text())
        self.assertEqual(receipt, self.receipt)

    def test_modified_manifest_rejected_before_extract(self):
        lock, directory = self.bundle()
        (directory / "fixture-sdk.manifest.json").write_text("{}")
        with self.assertRaises(ValueError):
            mod.install_bundle(lock, self.triplet, directory, self.destination)
        self.assertFalse(self.destination.exists())

    def test_modified_part_rejected(self):
        lock, directory = self.bundle()
        (directory / "fixture-sdk.zip").write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            mod.install_bundle(lock, self.triplet, directory, self.destination)

    def test_wrong_commit_rejected(self):
        lock, directory = self.bundle()
        lock["tested_commit"] = "f" * 40
        with self.assertRaises(ValueError):
            mod.install_bundle(lock, self.triplet, directory, self.destination)

    def test_nonempty_destination_preserved(self):
        lock, directory = self.bundle()
        self.destination.mkdir()
        (self.destination / "owned").write_text("keep")
        with self.assertRaises(ValueError):
            mod.install_bundle(lock, self.triplet, directory, self.destination)
        self.assertEqual((self.destination / "owned").read_text(), "keep")

    def test_thin_archive_rejected_atomically(self):
        (self.prefix / "lib/cef-static/cef_objects.a").write_bytes(b"!<thin>\n")
        lock, directory = self.bundle()
        with self.assertRaises(ValueError):
            mod.install_bundle(lock, self.triplet, directory, self.destination)
        self.assertFalse(self.destination.exists())

    def test_missing_required_payload_rejected(self):
        (self.prefix / "share/cef-static/resources/icudtl.dat").unlink()
        lock, directory = self.bundle()
        with self.assertRaises(ValueError):
            mod.install_bundle(lock, self.triplet, directory, self.destination)
        self.assertFalse(self.destination.exists())

    def test_unknown_package_owned_path_rejected(self):
        (self.prefix / "include/not-cef").write_text("unexpected")
        lock, directory = self.bundle()
        with self.assertRaises(ValueError):
            mod.install_bundle(lock, self.triplet, directory, self.destination)

    def test_unsafe_names(self):
        for name in ("a/../b", "/a", "a//b", "a\\b", "a:stream", "a/NUL", "a./b", "a\nb"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                mod.member_path(name)

    def test_lock_strict_fields(self):
        lock, directory = self.bundle()
        for change in (lambda x: x.update(schema=True), lambda x: x.update(url="file:///etc/passwd"),
                       lambda x: x.update(tested_commit="main"), lambda x: x["platforms"].pop(self.triplet)):
            bad = copy.deepcopy(lock)
            change(bad)
            with self.assertRaises(ValueError):
                mod.validate_lock(bad)

    def test_cached_asset_rechecked_without_network(self):
        lock, directory = self.bundle()
        record = lock["platforms"][self.triplet]["manifest"]
        with patch.object(mod.urllib.request, "build_opener", side_effect=AssertionError("network")):
            self.assertEqual(mod.acquire(record, directory, lock["tag"]), directory / record["name"])
            (directory / record["name"]).write_bytes(b"bad")
            with self.assertRaises(ValueError):
                mod.acquire(record, directory, lock["tag"])

    def test_unsafe_redirects(self):
        handler = mod.ReleaseRedirects()
        for url in ("http://github.com/a", "https://localhost/x", "https://evil.invalid/a", "https://a@github.com/x", "https://github.com:444/x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(None, None, 302, "", {}, url)

    def test_real_release_lock_structure(self):
        mod.validate_lock(json.loads((ROOT / "vcpkg/integration/release.lock.json").read_text()))


if __name__ == "__main__":
    unittest.main()
