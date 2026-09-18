"""Regression for the Windows platform-STL PartitionAlloc warning transform."""
from pathlib import Path
import importlib.util
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
PATCH = ROOT / "vcpkg/ports/cef-static/patch_source.py"
spec = importlib.util.spec_from_file_location("cef_patch_source", PATCH)
patch_source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch_source)


class PatchSourceTests(unittest.TestCase):
    def test_ctad_warning_remains_enabled_off_windows_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "base/allocator/partition_allocator/src/partition_alloc/BUILD.gn"
            path.parent.mkdir(parents=True)
            path.write_text(
                """config("dependants_extra_warnings") {
  if (build_with_chromium && is_clang && !is_fuchsia) {
    cflags = [
      "-Wc++11-narrowing",
      "-Wconditional-uninitialized",
      "-Wcstring-format-directive",
      "-Wctad-maybe-unsupported",
      "-Wdeprecated-copy",
      "-Wunused-but-set-variable",
      "-Wunused-macros",
    ]
  }
}
""",
                encoding="utf-8",
            )
            patch_source.EDITS.clear()
            patch_source.patch_windows_msvc_stl_warnings(root)
            changed = path.read_text()
            self.assertEqual(changed.count("-Wctad-maybe-unsupported"), 1)
            self.assertIn('if (!is_win) {', changed)
            self.assertIn('cflags += [ "-Wctad-maybe-unsupported" ]', changed)
            self.assertEqual(len(patch_source.EDITS), 2)


if __name__ == "__main__":
    unittest.main()
