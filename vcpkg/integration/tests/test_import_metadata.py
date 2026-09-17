"""Regressions for manager metadata from real exported vcpkg SDKs."""
from pathlib import PurePosixPath
import unittest
import test_sdk_import as fixture

mod = fixture.mod


class MetadataTests(unittest.TestCase):
    def test_both_triplets_relocate_only_manager_metadata(self):
        for source in mod.TRIPLETS.values():
            for name in mod.MANAGER_METADATA:
                with self.subTest(source=source, name=name):
                    actual = mod.selected_member(f"sdk/installed/{source}/share/cef-static/{name}", "sdk", source)
                    self.assertEqual(actual, mod.PROVENANCE_DIRECTORY / name)
            path = f"sdk/installed/{source}/share/cef-static/static-link-inventory.json"
            self.assertEqual(mod.selected_member(path, "sdk", source), PurePosixPath("share/cef-static/static-link-inventory.json"))

    def test_realistic_export_metadata_does_not_collide_with_new_abi(self):
        setup = fixture.ImportTests(methodName="test_source_receipt_is_preserved")
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        original = {"vcpkg_abi_info.txt": b"triplet old-abi\n", "vcpkg.spdx.json": b'{"name":"upstream-package"}'}
        for name, content in original.items():
            (setup.prefix / "share/cef-static" / name).write_bytes(content)
        lock, directory = setup.bundle()
        mod.install_bundle(lock, setup.triplet, directory, setup.destination)
        for name, content in original.items():
            self.assertEqual((setup.destination / mod.PROVENANCE_DIRECTORY / name).read_bytes(), content)
            fresh = setup.destination / "share/cef-static" / name
            self.assertFalse(fresh.exists())
            with fresh.open("xb") as stream:
                stream.write(b"new package ABI metadata")

    def test_reserved_destination_cannot_be_injected(self):
        for path in ("upstream-package-metadata/vcpkg_abi_info.txt", "UPSTREAM-PACKAGE-METADATA/vcpkg.spdx.json", "VCPKG_ABI_INFO.TXT"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                mod.selected_member("sdk/installed/x64-linux/share/cef-static/" + path, "sdk", "x64-linux")

    def test_versioned_shared_libraries_rejected(self):
        for name in ("libbad.so.1", "libbad.SO.1.2", "libbad.dylib", "bad.dll"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                mod.selected_member("sdk/installed/x64-linux/lib/cef-static/" + name, "sdk", "x64-linux")


if __name__ == "__main__":
    unittest.main()
