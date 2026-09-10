"""Small parser/contract tests. These do NOT build or validate the CEF engine."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/'export_static.py'
spec = importlib.util.spec_from_file_location('export_static', SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LinkEdgeTests(unittest.TestCase):
    def test_direct_and_implicit_inputs_not_order_only(self):
        query = '''cef_static_smoke:
  input: link
    obj/cef/cef_static_smoke/smoke.o
    obj/cef/cef_engine.a
    | obj/rust/libcore.rlib
    || obj/code_generator.stamp
    || obj/unused.a
  outputs:
    all
'''
        self.assertEqual(module.query_link_inputs(query), [
            'obj/cef/cef_static_smoke/smoke.o','obj/cef/cef_engine.a','obj/rust/libcore.rlib'])

    def test_shared_engine_rejected(self):
        for filename in ['libcef.so','libcef.dll.lib','libEGL.so','libGLESv2.dll.lib','chrome_elf.dll.lib']:
            with self.subTest(filename=filename), self.assertRaises(RuntimeError):
                module.query_link_inputs('app:\n  input: link\n    '+filename+'\n  outputs:\n')

    def test_order_only_dll_is_not_a_link_input(self):
        query = ('app:\n  input: link\n    obj/cef/cef_engine.a\n'
                 '    || libEGL.so\n    || libGLESv2.dll\n  outputs:\n')
        self.assertEqual(module.query_link_inputs(query), ['obj/cef/cef_engine.a'])

    def test_implicit_shared_toc_is_rejected(self):
        for filename in ['custom.so.TOC', 'libcef.so.TOC', 'third_party.dll.lib']:
            with self.subTest(filename=filename), self.assertRaises(RuntimeError):
                module.query_link_inputs('app:\n  input: link\n    obj/a.a\n    | '+filename+'\n  outputs:\n')

    def test_other_shared_libraries_rejected(self):
        with self.assertRaises(RuntimeError):
            module.query_link_inputs('app:\n  input: link\n    custom.so.1\n  outputs:\n')

    def test_empty_is_failure(self):
        with self.assertRaises(RuntimeError):
            module.query_link_inputs('app:\n  input: link\n    obj/a.stamp\n  outputs:\n')

    def test_reference_object_detection(self):
        for path in ['obj/cef/cef_static_smoke/smoke.o','obj/cef/cef_static_smoke.smoke.obj']:
            self.assertTrue(module.smoke_object(Path(path)))
        self.assertFalse(module.smoke_object(Path('obj/cef/cef_engine/smoke.o')))

    def test_semantic_windows_options(self):
        flags = ['/STACK:0x800000','/INCLUDE:important_initializer','/guard:cf','/lldignoreenv']
        kept, discarded = module.link_options(flags, True, lambda p: p)
        self.assertEqual(kept,flags[:3]); self.assertEqual(len(discarded),1)

    def test_semantic_linux_options(self):
        flags = ['-Wl,-z,now','-Wl,--export-dynamic-symbol=malloc','--sysroot=../../sysroot']
        kept, discarded = module.link_options(flags, False, lambda p: p)
        self.assertEqual(kept,flags[:2]); self.assertEqual(len(discarded),1)

    def test_link_scripts_relocated(self):
        kept, _ = module.link_options(['-Wl,--version-script=../../example.list'],False,
                                      lambda p:'${_cef_static_prefix}/share/cef-static/linker/example.list')
        self.assertNotIn('../',kept[0])
        self.assertIn('${_cef_static_prefix}',kept[0])

    def test_unknown_options_fail_closed(self):
        for windows, flag in [(True,'/UNREVIEWED'),(False,'-Wl,--unreviewed')]:
            with self.assertRaises(RuntimeError):
                module.link_options([flag], windows, lambda p:p)

    def test_cmake_injection_rejected(self):
        with self.assertRaises(RuntimeError):
            module.quote_cmake('library;unintended-target')


@unittest.skipUnless(shutil.which('cmake'), 'CMake required for port precondition tests')
class NativeTripletTests(unittest.TestCase):
    def check_case(self, *, target, host, host_os, should_succeed):
        # CMake variables are synthetic: this tests policy, not a native build.
        preconditions = (SCRIPT.parent/'portfile.cmake').read_text().split('set(VCPKG_BUILD_TYPE release)')[0]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'test.cmake'
            path.write_text('\n'.join([
                'set(VCPKG_TARGET_ARCHITECTURE x64)',
                'set(VCPKG_LIBRARY_LINKAGE static)',
                'set(VCPKG_CRT_LINKAGE static)',
                'set(VCPKG_CROSSCOMPILING TRUE)',
                f'set(VCPKG_HOST_TRIPLET {host})',
                f'set(CMAKE_HOST_SYSTEM_NAME {host_os})',
                f'set(CMAKE_HOST_WIN32 {"TRUE" if host_os == "Windows" else "FALSE"})',
                f'set(VCPKG_TARGET_IS_WINDOWS {"TRUE" if target == "Windows" else "FALSE"})',
                f'set(VCPKG_TARGET_IS_LINUX {"TRUE" if target == "Linux" else "FALSE"})',
                preconditions]))
            result = subprocess.run(['cmake', '-P', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode == 0, should_succeed, result.stdout+result.stderr)

    def test_windows_static_target_dynamic_host_triplet(self):
        self.check_case(target='Windows', host='x64-windows', host_os='Windows', should_succeed=True)

    def test_linux_native(self):
        self.check_case(target='Linux', host='x64-linux', host_os='Linux', should_succeed=True)

    def test_cross_os_rejected(self):
        self.check_case(target='Windows', host='x64-linux', host_os='Linux', should_succeed=False)

    def test_cross_architecture_rejected(self):
        self.check_case(target='Windows', host='arm64-windows', host_os='Windows', should_succeed=False)


class ReceiptTests(unittest.TestCase):
    def test_hybrid_receipt_never_accepted(self):
        receipt={'cef_commit':module.CEF_COMMIT,'chromium_commit':module.CHROMIUM_COMMIT,
                 'source_build_verified':True,'engine_linkage':'shared'}
        with self.assertRaises(RuntimeError): module.verify_reference(receipt)

    def test_missing_execution_never_accepted(self):
        receipt={'cef_commit':module.CEF_COMMIT,'chromium_commit':module.CHROMIUM_COMMIT,
                 'source_build_verified':False,'engine_linkage':'static'}
        with self.assertRaises(RuntimeError): module.verify_reference(receipt)

    def test_same_process_rejected(self):
        # Explicitly synthetic values, used only to test the receipt validator.
        receipt={'cef_commit':module.CEF_COMMIT,'chromium_commit':module.CHROMIUM_COMMIT,
                 'source_build_verified':True,'engine_linkage':'static',
                 'smoke': {'cef':module.CEF_VERSION,'engine':'static',
                           'javascript':True,'paint':True,'browser_modules_clean':True,
                           'renderer_modules_clean':True,'browser_pid':1,'renderer_pid':1}}
        with self.assertRaises(RuntimeError): module.verify_reference(receipt)

    def test_no_fabricated_success_from_empty_receipt(self):
        with self.assertRaises(RuntimeError): module.verify_reference({})


if __name__=='__main__': unittest.main()
