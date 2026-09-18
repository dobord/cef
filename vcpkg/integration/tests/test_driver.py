"""No Chromium compilation; real miniature filesystem/checkpoint contracts."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "vcpkg/integration"))
import driver


class DriverTests(unittest.TestCase):
    def test_unchanged_and_rebuilt_outputs_are_distinguished(self):
        before = {"a.o": ["1", "hash", 10, 100]}
        self.assertEqual(driver.progress(before, before, False)["status"], "stalled")
        rebuilt = {"a.o": ["1", "hash", 10, 200]}
        report = driver.progress(before, rebuilt, False)
        self.assertEqual(report["changed_outputs"], 1)
        self.assertEqual(report["new_outputs"], 0)
        self.assertEqual(report["status"], "progress")

    def test_complete_no_op_can_be_revalidated(self):
        report = driver.progress({}, {}, True)
        self.assertEqual(report["status"], "progress")
        self.assertFalse(report["engine_runtime_verified"])

    def test_invalid_contract_and_retry_rejected(self):
        with self.assertRaises(ValueError):
            driver.identity(Path("/unused"), "not a hash")
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}):
            with self.assertRaises(ValueError):
                driver.slice_build(Path("/unused"), Path("/unused"), Path("/unused"), Path("/unused"), "a" * 64, 1, 1)

    def test_ninja_output_state_reads_real_mtimes(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            out = work / "download/chromium/src/out/CEF_Static_Release_x64"
            out.mkdir(parents=True)
            obj = out / "fixture.o"
            obj.write_bytes(b"synthetic")
            (out / ".ninja_log").write_text("# ninja log v5\n0\t1\t1\tfixture.o\thash\n")
            before = driver.ninja_state(work)
            os.utime(obj, ns=(1000000000, 2000000000))
            self.assertNotEqual(before, driver.ninja_state(work))

    def test_strict_linux_dependency_audit(self):
        good = " 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]\n" \
               " 0x0000000000000001 (NEEDED)             Shared library: [libm.so.6]\n"
        self.assertEqual(driver.strict_linux_dependencies(good), ["libc.so.6", "libm.so.6"])
        with self.assertRaises(ValueError):
            driver.strict_linux_dependencies(good + " Shared library: [libX11.so.6]\n")

    def test_strict_installer_never_rebrands_release_import(self):
        text = (ROOT / "vcpkg/integration/install.cmake").read_text()
        self.assertIn('cef-static[strict-platform]', text)
        self.assertIn('The published engine-only SDK cannot be rebranded as static-third-party', text)
        self.assertIn('CEF_STATIC_PLATFORM_MANIFEST', text)
        self.assertIn('CEF_Static_Platform_Release_x64', text)

    def test_atomic_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub/state.json"
            driver.write_json(path, {"ready": False})
            self.assertTrue(path.exists())
            self.assertFalse(path.with_name("state.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
