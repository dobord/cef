"""Regression coverage for the static engine/client C ABI boundary."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cpp_client', ROOT/'cpp_client.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class CppClientTests(unittest.TestCase):
    def test_namespace_isolation_does_not_change_c_structs_or_literals(self):
        original = '''#include "include/base/cef_logging.h"
// namespace base { and cef:: are documentation
const char* text = "namespace cef { base:: shutdown_checker::";
const char* raw = R"tag("base::" namespace cef { shutdown_checker::)tag";
struct cef_app_t { cef_base_ref_counted_t base; };
namespace base { void f(); }
namespace cef::logging { void f(); }
namespace shutdown_checker { void f(); }
void g() { base::f(); cef::logging::f(); shutdown_checker::f(); app.base.add_ref(&app.base); }
'''
        result = module.isolate_namespaces(original)
        self.assertIn('#include "include/base/cef_logging.h"', result)
        self.assertIn('// namespace base { and cef:: are documentation', result)
        self.assertIn('"namespace cef { base:: shutdown_checker::"', result)
        self.assertIn('R"tag("base::" namespace cef { shutdown_checker::)tag"', result)
        self.assertIn('cef_base_ref_counted_t base;', result)
        self.assertIn('app.base.add_ref(&app.base)', result)
        self.assertIn('namespace cef_static_client_base {', result)
        self.assertIn('namespace cef_static_client::logging', result)
        self.assertIn('cef_static_client_shutdown_checker::f()', result)

    def test_staging_uses_generated_sources_and_preserves_c_api_macros(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source, prefix = root/'source', root/'prefix'
            (source/'libcef_dll').mkdir(parents=True)
            include = prefix/'include/cef-static/include'
            include.mkdir(parents=True)
            (include/'base').mkdir()
            (include/'base/cef_compiler_specific.h').write_text(
                '#define TRIVIAL_ABI [[clang::trivial_abi]]\n')
            (include/'cef_app.h').write_text(
                '#define CefWindowHandle cef_window_handle_t\n'
                'class CefApp {}; class CefBrowserHost {};\n'
                'int CefExecuteProcess(); bool CefInitialize();\n')
            groups = {}
            for index, key in enumerate(('libcef_dll_wrapper_sources_base',
                    'libcef_dll_wrapper_sources_common', 'autogen_client_side',
                    'libcef_dll_wrapper_sources_win')):
                filename = f'libcef_dll/source{index}.cc'
                (source/filename).write_text('namespace base { void f(); }\n')
                groups[key] = [filename]
            (source/'cef_paths.gypi').write_text(repr({'variables': groups}))
            (source/'cef_paths2.gypi').write_text(repr({'variables': {}}))
            module.stage(source, prefix)
            names = (prefix/'include/cef-static-cpp/include/cef_static_client_names.h').read_text()
            self.assertIn('#define CefExecuteProcess CefStaticClientExecuteProcess', names)
            self.assertNotIn('#define CefWindowHandle', names)
            self.assertIn('[[clang::trivial_abi]]',
                          (include/'base/cef_compiler_specific.h').read_text())
            self.assertNotIn('[[clang::trivial_abi]]',
                (prefix/'include/cef-static-cpp/include/base/cef_compiler_specific.h').read_text())
            self.assertEqual((include/'cef_app.h').read_text(),
                (prefix/'include/cef-static-cpp/include/cef_app.h').read_text())
            staged = prefix/'share/cef-static/cpp-client'
            self.assertIn('autogen_client_side', (staged/'sources.cmake').read_text())
            self.assertIn('namespace cef_static_client_base',
                          (staged/'libcef_dll/source2.cc').read_text())
            with self.assertRaises(ValueError):
                module.stage(source, prefix)


if __name__ == '__main__':
    unittest.main()
