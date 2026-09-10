"""Small parser/contract tests. These do NOT build or validate the CEF engine."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import tempfile
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
