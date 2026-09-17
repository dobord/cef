"""Synthetic transport tests; no real CEF runtime certificate is generated here."""
import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import canonical, digest, validate_lock, semantic_key
import release_import
from iteration import progress

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("synthetic_package_fixtures", ROOT / "static/tests/test_sdk_package.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.pkg = release_import.validator()
        (self.source / "static-sdk-receipt.json").write_bytes(canonical(fixtures.synthetic_receipt("x64-linux")))
        for path, value in {
            "share/cef-static/cef-static-config.cmake": "# synthetic CMake",
            "share/cef-static/resources/icudtl.dat": "synthetic data",
            "lib/cef-static/test.a": "!<arch>\n",
            "include/cef-static/include/test.h": "/* synthetic */"}.items():
            file = self.source / "installed/x64-linux" / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(value)
        manifest = self.pkg.package_sdk(self.source, self.root / "assets", "cef-test", part_bytes=1024)
        release = {"tag": "cef-test", "commit": fixtures.COMMIT, "name": "cef-test", "manifest_sha256": digest(manifest),
                   "assets": [{"name": p.name, "size": p.stat().st_size, "sha256": digest(p)} for p in sorted(manifest.parent.iterdir())]}
        self.lock = {"schema": 1, "recipe_sha": "c"*40, "cef_sha": self.pkg.CEF, "chromium_sha": self.pkg.CHROMIUM,
                     "acquisition": "release", "platforms": {
                         "linux": {"profile": "static-third-party", "triplet": "x64-linux-static-release", "release": release},
                         "windows": {"profile": "static-third-party", "triplet": "x64-windows-static-release", "release": copy.deepcopy(release)}}}
    def test_verified_multipart_import(self):
        with mock.patch.object(release_import, "fetch_assets", return_value=self.root / "assets"):
            result = release_import.import_release(self.lock, "linux", self.root / "downloads", self.root / "package")
        self.assertFalse(result["target_runtime_verified"])
        self.assertTrue((self.root / "package/lib/cef-static/test.a").exists())
        self.assertFalse((self.root / "package/installed").exists())
    def test_wrong_producer_refused(self):
        self.lock["platforms"]["linux"]["release"]["commit"] = "d"*40
        with mock.patch.object(release_import, "fetch_assets", return_value=self.root / "assets"), self.assertRaises(ValueError):
            release_import.import_release(self.lock, "linux", self.root / "downloads", self.root / "package")
        self.assertFalse((self.root / "package").exists())
    def test_no_mutable_latest_and_no_fallback(self):
        for mode in ("auto", "latest", "source-resume"):
            lock = copy.deepcopy(self.lock); lock["acquisition"] = mode
            with self.assertRaises(ValueError): validate_lock(lock)
    def test_platform_identity_changes(self):
        self.assertNotEqual(semantic_key(self.lock, "linux"), semantic_key(self.lock, "windows"))
        before = semantic_key(self.lock, "linux")
        self.lock["platforms"]["linux"]["profile"] = "engine-static"
        self.assertNotEqual(before, semantic_key(self.lock, "linux"))
    def test_recompiled_existing_output_is_progress(self):
        self.assertTrue(progress({"a.o":"1:x"}, {"a.o":"2:y"}, False)["progress"])
        self.assertFalse(progress({"a.o":"1:x"}, {"a.o":"1:x"}, False)["progress"])
        self.assertTrue(progress({}, {}, True)["progress"])

if __name__ == "__main__": unittest.main()
