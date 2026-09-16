"""Native delay-load policy regression; fixture DLLs are NOT a CEF SDK."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('delay_export', ROOT/'vcpkg/ports/cef-static/export_static.py')
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)
FLAGS = ['/WX', '/OPT:REF', '/INCREMENTAL:NO', '/DELAYLOAD:cef_delay_used.dll',
         '/DELAYLOAD:cef_delay_optional.dll', '/ignore:4199']


def options(flags):
    def unexpected_file(path):
        raise AssertionError('Windows delay policy must not stage linker scripts: '+path)
    return export.link_options(flags, True, unexpected_file)


class DelayPolicyTests(unittest.TestCase):
    def test_preserves_exact_upstream_exception_and_all_delay_declarations(self):
        kept, omitted = options(FLAGS)
        self.assertEqual(kept, FLAGS)
        self.assertEqual(omitted, [])
        actual = ['/WX', '/ignore:4199', '/DELAYLOAD:shcore.dll',
                  '/DELAYLOAD:api-ms-win-shcore-scaling-l1-1-1.dll', '/guard:cf']
        self.assertEqual(options(actual)[0], actual)

    def test_exception_is_not_injected_or_kept_without_delay_load(self):
        self.assertNotIn('/ignore:4199', options(FLAGS[:-1])[0])
        kept, omitted = options(['/WX', '/ignore:4199'])
        self.assertEqual(kept, ['/WX'])
        self.assertEqual([entry['flag'] for entry in omitted], ['/ignore:4199'])

    def test_other_warning_suppressions_are_not_exported(self):
        for flag in ['/ignore:4221', '/ignore:4044', '/ignore:4199,4044', '/ignore:41990']:
            with self.subTest(flag=flag):
                kept, omitted = options(FLAGS+[flag])
                self.assertEqual(kept, FLAGS)
                self.assertEqual([entry['flag'] for entry in omitted], [flag])
        with self.assertRaises(RuntimeError):
            options(FLAGS+['/WX:NO'])

    def test_case_insensitive_exact_match_and_deduplication(self):
        flags = ['/WX', '/IgNoRe:4199', '/dElAyLoAd:shcore.dll', '/IgNoRe:4199']
        self.assertEqual(options(flags)[0], flags[:3])

    def test_forbidden_engine_delay_load_still_rejected(self):
        for dll in ['libcef.dll', 'chrome_elf.dll', 'libegl.dll', 'dxcompiler.dll']:
            with self.subTest(dll=dll), self.assertRaises(RuntimeError):
                options(FLAGS+['/DELAYLOAD:'+dll])

    def test_linux_does_not_accept_the_windows_exception(self):
        with self.assertRaises(RuntimeError):
            export.link_options(['/ignore:4199'], False, lambda path: path)
        self.assertEqual(export.link_options(['-Wl,--fatal-warnings'], False, lambda p: p)[0],
                         ['-Wl,--fatal-warnings'])


@unittest.skipUnless(os.name == 'nt', 'MSVC delay-import and loader tests require Windows')
class NativeDelayPolicyTests(unittest.TestCase):
    def test_optional_and_used_imports_with_strict_native_linker(self):
        logs = ROOT/'static-diagnostics/windows-delayload-regression'
        logs.mkdir(parents=True, exist_ok=True)
        proof = logs/'result.json'; proof.unlink(missing_ok=True)
        tools = {name: shutil.which(name) for name in ['cmake', 'cl.exe', 'dumpbin.exe', 'ninja']}
        self.assertTrue(all(tools.values()), str(tools))
        report = {'fixture_only': True, 'cef_sdk_verified': False, 'tools': tools,
                  'original_flags': FLAGS, 'exported_flags': options(FLAGS)[0], 'commands': []}
        def record():
            proof.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        record()
        with tempfile.TemporaryDirectory(prefix='cef delay policy ', dir=os.environ.get('RUNNER_TEMP')) as temp:
            root = Path(temp); source = root/'source with spaces'; source.mkdir()
            def run(args, label, cwd=root):
                p = subprocess.run(list(map(str, args)), cwd=cwd, capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', timeout=120)
                (logs/(label+'.log')).write_text(p.stdout+p.stderr, encoding='utf-8')
                report['commands'].append({'label': label, 'exit_code': p.returncode})
                record()
                return p
            def ok(args, label):
                p = run(args, label); self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
                return p
            for name in ('used', 'optional'):
                (source/(name+'.c')).write_text(
                    f'__declspec(dllexport) int {name}_value(void) {{ return 42; }}\n')
                (source/(name+'_edge.c')).write_text(
                    f'__declspec(dllimport) int {name}_value(void);\n'
                    f'int probe_{name}(void) {{ return {name}_value(); }}\n')
            (source/'main.c').write_text('''#include <windows.h>
#if !defined(_MT) || defined(_DLL)
#error Static CRT required
#endif
int probe_used(void);
int probe_optional(void);
int main(void) {
  if (GetModuleHandleA("cef_delay_used.dll") || GetModuleHandleA("cef_delay_optional.dll")) return 10;
  if (probe_used() != 42 || !GetModuleHandleA("cef_delay_used.dll")) return 11;
#ifdef USE_OPTIONAL
  if (probe_optional() != 42 || !GetModuleHandleA("cef_delay_optional.dll")) return 12;
#else
  if (GetModuleHandleA("cef_delay_optional.dll")) return 13;
#endif
  return 0;
}
''')
            fixed, _ = options(FLAGS)
            legacy = [flag for flag in fixed if flag.lower() != '/ignore:4199']
            (source/'policy.cmake').write_text(
                'if(LEGACY)\n  set(policy '+ ' '.join(export.quote_cmake(f) for f in legacy)+')\n'
                'else()\n  set(policy '+ ' '.join(export.quote_cmake(f) for f in fixed)+')\nendif()\n')
            (source/'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.24)
project(delay_policy LANGUAGES C)
set(CMAKE_MSVC_RUNTIME_LIBRARY MultiThreaded)
include(policy.cmake)
add_library(used SHARED used.c)
add_library(optional SHARED optional.c)
set_target_properties(used PROPERTIES OUTPUT_NAME cef_delay_used)
set_target_properties(optional PROPERTIES OUTPUT_NAME cef_delay_optional)
add_library(closure STATIC used_edge.c optional_edge.c)
foreach(target IN ITEMS minimal full)
  add_executable(${target} main.c)
  target_compile_options(${target} PRIVATE /W4 /WX)
  target_link_options(${target} PRIVATE ${policy})
  target_link_libraries(${target} PRIVATE closure optional delayimp)
  if(NOT MISSING_SYMBOL)
    target_link_libraries(${target} PRIVATE used)
  endif()
  if(OTHER_WARNING)
    target_link_options(${target} PRIVATE /CEF_UNRECOGNIZED_LINK_OPTION)
  endif()
endforeach()
target_compile_definitions(full PRIVATE USE_OPTIONAL)
''')
            def configure(label, generator, *extra):
                build = root/label
                args = [tools['cmake'], '-S', source, '-B', build, '-G', generator]
                args += ['-A', 'x64'] if generator.startswith('Visual') else ['-DCMAKE_BUILD_TYPE=Release']
                ok(args+list(extra), label+'-configure')
                return build
            def build(where, label):
                return run([tools['cmake'], '--build', where, '--config', 'Release'], label)
            for generator, name in [('Visual Studio 17 2022', 'msbuild'), ('Ninja', 'ninja')]:
                old = configure(name+'-legacy', generator, '-DLEGACY=ON')
                p = build(old, name+'-legacy-build')
                self.assertNotEqual(p.returncode, 0)
                self.assertIn('LNK4199', p.stdout+p.stderr)
                self.assertIn('LNK1218', p.stdout+p.stderr)
                where = configure(name+'-fixed', generator)
                p = build(where, name+'-fixed-build'); self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
                directory = where/'Release' if name == 'msbuild' else where
                for target in ('minimal', 'full'):
                    exe = directory/(target+'.exe')
                    ok([exe], name+'-'+target+'-run')
                    imports = ok([tools['dumpbin.exe'], '/IMPORTS', exe], name+'-'+target+'-imports').stdout.lower()
                    normal, sep, delayed = imports.partition('section contains the following delay load imports:')
                    self.assertTrue(sep, imports)
                    self.assertNotIn('cef_delay_used.dll', normal)
                    self.assertIn('cef_delay_used.dll', delayed)
                    self.assertNotIn('cef_delay_optional.dll', normal)
                    self.assertEqual('cef_delay_optional.dll' in delayed, target == 'full')
                report[name+'_delay_imports_and_lazy_load_verified'] = True; record()
            warning = configure('other-warning', 'Visual Studio 17 2022', '-DOTHER_WARNING=ON')
            p = build(warning, 'other-warning-build')
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('LNK4044', p.stdout+p.stderr)
            self.assertIn('LNK1218', p.stdout+p.stderr)
            missing = configure('missing-symbol', 'Visual Studio 17 2022', '-DMISSING_SYMBOL=ON')
            p = build(missing, 'missing-symbol-build')
            self.assertNotEqual(p.returncode, 0)
            self.assertRegex(p.stdout+p.stderr, r'LNK(?:2001|2019)')
            report.update(other_warning_rejected=True, missing_symbol_rejected=True, status='success')
            record()


if __name__ == '__main__': unittest.main()
