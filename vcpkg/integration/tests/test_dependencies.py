import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from static_dependencies import resolve_flags, platform_cmake, apply
from release_import import selected_payload
from common import relative

class StaticDependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "lib").mkdir()
        (self.root / "lib/libtest.a").write_bytes(b"!<arch>\n")
    def tearDown(self):
        self.tmp.cleanup()
    def test_absolute_static_only(self):
        libs, options, system = resolve_flags(["-L" + str(self.root / "lib"), "-ltest", "-lm", "-pthread"], self.root)
        self.assertEqual(libs, ["lib/libtest.a"])
        self.assertEqual(options, ["-pthread"])
        self.assertEqual(system, ["m"])
    def test_no_system_fallback(self):
        (self.root / "lib/libtest.a").unlink()
        (self.root / "lib/libtest.so").write_bytes(b"ELF")
        with self.assertRaises(ValueError): resolve_flags(["-ltest"], self.root)
    def test_reject_search_path_escape(self):
        with self.assertRaises(ValueError): resolve_flags(["-L/usr/lib", "-ltest"], self.root)
    def test_reject_thin_archive(self):
        (self.root / "lib/libtest.a").write_bytes(b"!<thin>\n")
        with self.assertRaises(ValueError): resolve_flags(["-ltest"], self.root)
    def test_unreviewed_flags(self):
        for flag in ("@evil.rsp", "-Wl,-rpath,/tmp", "-l:libtest.so", "-ltest;bad", "-framework"):
            with self.subTest(flag=flag), self.assertRaises(ValueError): resolve_flags([flag], self.root)
    def test_only_cef_owned_payload(self):
        self.assertEqual(selected_payload("cef-test/installed/x64-linux/lib/cef-static/a.a", "cef-test", "x64-linux"), "lib/cef-static/a.a")
        for path in ("cef-test/installed/vcpkg/status", "cef-test/installed/x64-linux/lib/another.a", "cef-test/scripts/buildsystems/vcpkg.cmake"):
            self.assertIsNone(selected_payload(path, "cef-test", "x64-linux"))
    def test_invalid_member_paths(self):
        for path in ("../x", "x//y", "x/./y", "C:/x", "x/.git/config", "x/NUL.txt", "x\\y"):
            with self.subTest(path=path), self.assertRaises(ValueError): relative(path)
    def test_external_group_follows_engine(self):
        share = self.root / "share/cef-static"
        share.mkdir(parents=True)
        (share / "static-link-inventory.json").write_text('{"system_libraries":["z"]}')
        (share / "cef-static-config.cmake").write_text('set_property(TARGET CEF::static PROPERTY INTERFACE_LINK_LIBRARIES\n  "engine"\n  "z"\n)\n')
        contract = {"archives": [{"path": "lib/libtest.a"}], "os_libraries": [], "link_options": []}
        with mock.patch("static_dependencies.resolve", return_value=contract):
            apply(self.root, self.root, self.root / "pkgconf")
        text = (share / "cef-static-config.cmake").read_text()
        self.assertGreater(text.index("APPEND PROPERTY INTERFACE_LINK_LIBRARIES CEF::platform"), text.index('"engine"'))
        with self.assertRaises(ValueError):
            apply(self.root, self.root, self.root / "pkgconf")
    @unittest.skipUnless(os.name != "nt" and shutil.which("cc") and shutil.which("ar") and shutil.which("cmake"), "native Linux C tools required")
    def test_native_relocated_static_consumer(self):
        # The test links and executes; inspecting CMake text is not enough.
        source = self.root / "answer.c"
        source.write_text("int answer(void) { return 42; }\n")
        subprocess.run(["cc", "-c", str(source), "-o", str(self.root / "answer.o")], check=True)
        archive = self.root / "lib/libanswer.a"
        subprocess.run(["ar", "rcs", str(archive), str(self.root / "answer.o")], check=True)
        cfg = self.root / "platform.cmake"
        cfg.write_text(platform_cmake({"archives": [{"path": "lib/libanswer.a"}], "os_libraries": [], "link_options": []}))
        project = self.root / "consumer"
        project.mkdir()
        (project / "main.c").write_text("int answer(void); int main(void) { return answer()!=42; }\n")
        (project / "CMakeLists.txt").write_text('cmake_minimum_required(VERSION 3.24)\nproject(test LANGUAGES C)\nget_filename_component(_cef_static_prefix "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)\ninclude("${_cef_static_prefix}/platform.cmake")\nadd_executable(probe main.c)\ntarget_link_libraries(probe PRIVATE CEF::platform)\n')
        moved = self.root.parent / (self.root.name + " moved")
        shutil.copytree(self.root, moved)
        try:
            shutil.rmtree(self.root)
            subprocess.run(["cmake", "-S", str(moved / "consumer"), "-B", str(moved / "build")], check=True, capture_output=True)
            subprocess.run(["cmake", "--build", str(moved / "build")], check=True, capture_output=True)
            subprocess.run([str(moved / "build/probe")], check=True)
        finally:
            shutil.rmtree(moved)

if __name__ == "__main__": unittest.main()
