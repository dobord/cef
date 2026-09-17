"""Public headers/license directories are not Debug build configurations."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sdk_import

class PathsTests(unittest.TestCase):
    def test_legal_nested_debug_names(self):
        for relative in ("include/cef-static/include/base/debug/alias.h",
                         "share/cef-static/licenses/third_party/example/debug/LICENSE"):
            self.assertEqual(sdk_import.selected_member("sdk/installed/x64-linux/" + relative, "sdk", "x64-linux").as_posix(), relative)

    def test_debug_configuration_and_workspace_still_rejected(self):
        for relative in ("debug/lib/cef-static/example.a", "lib/cef-static/buildtrees/state.a",
                         "share/cef-static/.git/config"):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                sdk_import.selected_member("sdk/installed/x64-linux/" + relative, "sdk", "x64-linux")

if __name__ == "__main__":
    unittest.main()
