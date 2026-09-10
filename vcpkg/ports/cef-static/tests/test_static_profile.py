"""Profile/import regressions from actual run 34501838409, not a build receipt."""
import importlib.util
from pathlib import Path
import unittest

PORT = Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location(name, PORT/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
builder = load('source_build')
exporter = load('export_static')

class ProfileTests(unittest.TestCase):
    def test_linux_drops_only_excluded_installer_argument(self):
        original = {'enable_linux_installer': False, 'cef_static_engine': True,
                    'use_static_angle': True, 'enable_vulkan': False,
                    'a_future_unreviewed_argument': 'must stay visible to GN'}
        actual = builder.static_profile(original, False)
        self.assertNotIn('enable_linux_installer', actual)
        self.assertIn('enable_linux_installer', original)
        self.assertEqual(actual, {k:v for k,v in original.items() if k!='enable_linux_installer'})
    def test_windows_excludes_built_dxc_and_agility_dlls(self):
        original = {'is_component_build': False, 'cef_static_engine': True}
        actual = builder.static_profile(original, True)
        self.assertFalse(actual['dawn_use_built_dxc'])
        self.assertFalse(actual['dawn_use_agility_sdk'])
        self.assertTrue(actual['dawn_force_system_component_load'])
        self.assertFalse(actual['is_component_build'])
        self.assertEqual(len(original), 2)
    def test_dxc_import_library_is_still_rejected(self):
        for filename in ['dxcompiler.dll.lib', 'dxil.dll.lib', 'engine.so.1', 'engine.so.TOC']:
            with self.subTest(filename=filename), self.assertRaises(RuntimeError):
                exporter.query_link_inputs('app:\n  input: link\n    '+filename+'\n  outputs:\n')
    def test_dll_main_source_object_is_not_an_import_library(self):
        filename = 'obj/third_party/swiftshader/src/WSI/WSI_export.dll_main.obj'
        self.assertEqual(exporter.query_link_inputs('app:\n  input: link\n    '+filename+'\n  outputs:\n'), [filename])
    def test_gn_unused_argument_check_is_not_disabled(self):
        self.assertIn('--fail-on-unused-args', builder.gn_generate_command(Path('gn'), Path('out')))

if __name__=='__main__': unittest.main()
