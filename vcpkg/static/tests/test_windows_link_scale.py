"""Large real COFF/CMake closure regression. This fixture is NOT a CEF SDK test."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('scaled_export', ROOT/'vcpkg/ports/cef-static/export_static.py')
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)
COUNT = 2600  # exceeds the observed 2344 archives; every unique symbol is used


def names(count=COUNT):
    return [f'cef_{i:04d}_'+hashlib.sha256(str(i).encode()).hexdigest()[:12]+'.lib' for i in range(count)]


def config(libraries, *, legacy=False):
    lines = ['get_filename_component(_cef_static_prefix "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)',
             'add_library(CEF::static INTERFACE IMPORTED GLOBAL)',
             'add_library(CEF::objects STATIC IMPORTED GLOBAL)',
             'set_property(TARGET CEF::objects PROPERTY IMPORTED_LOCATION "${_cef_static_prefix}/lib/cef-static/cef_objects.lib")']
    if legacy:
        for i, name in enumerate(libraries):
            lines += [f'add_library(CEF::archive_{i} STATIC IMPORTED GLOBAL)',
                      f'set_property(TARGET CEF::archive_{i} PROPERTY IMPORTED_LOCATION "${{_cef_static_prefix}}/lib/cef-static/{name}")']
    else:
        lines += export.windows_archive_targets(libraries)
    lines += ['set_property(TARGET CEF::static PROPERTY INTERFACE_LINK_LIBRARIES',
              '  "$<LINK_LIBRARY:WHOLE_ARCHIVE,CEF::objects>"',
              *[f'  CEF::archive_{i}' for i in range(len(libraries))], ')']
    return '\n'.join(lines)+'\n'


class LinkContractTests(unittest.TestCase):
    def test_all_names_and_order_retained_without_absolute_link_inputs(self):
        libs = names()
        result = '\n'.join(export.windows_archive_targets(libs))
        actual = re.findall(r'PROPERTY IMPORTED_LIBNAME "([^"]+)"', result)
        self.assertEqual(actual, libs)
        self.assertEqual(result.count('PROPERTY INTERFACE_LINK_DEPENDS'), COUNT)
        self.assertEqual(result.count('PROPERTY INTERFACE_LINK_DIRECTORIES'), COUNT)
        self.assertNotIn('PROPERTY IMPORTED_LOCATION', result)
        self.assertNotIn('WHOLE_ARCHIVE', result)  # only the original forced object bundle uses it
        self.assertNotIn('.rsp', result)
        self.assertLess(sum(len(n)+3 for n in libs), 80*1024)

    def test_unsafe_names_duplicates_empty_and_budget_rejected(self):
        good = names(1)[0]
        for bad in ([], [good, good], ['a.lib'], ['../'+good], ['x/'+good],
                    ['@'+good], [good+'\n/INCLUDE:evil'], [good+';other'],
                    [good.replace('.lib', '.dll')], names(4000)):
            with self.subTest(bad=str(bad)[:70]), self.assertRaises(RuntimeError):
                export.windows_archive_targets(bad)

    def test_exporter_routes_windows_and_retains_linux_and_forced_objects(self):
        text = (ROOT/'vcpkg/ports/cef-static/export_static.py').read_text()
        self.assertIn('cmake += windows_archive_targets(windows_archive_names)', text)
        self.assertIn("'$<LINK_LIBRARY:WHOLE_ARCHIVE,CEF::objects>'", text)
        self.assertIn("'$<LINK_GROUP:RESCAN,'", text)
        self.assertIn('materialize_windows_thin_archive(ar, path, dest, source, diagnostics)', text)


@unittest.skipUnless(os.name == 'nt', 'real MSVC/MSBuild regression runs on Windows')
class NativeLinkScaleTests(unittest.TestCase):
    def test_full_size_native_closure_relocation_and_dependency_tracking(self):
        logs = ROOT/'static-diagnostics/windows-link-scale'
        logs.mkdir(parents=True, exist_ok=True)
        proof = logs/'result.json'; proof.unlink(missing_ok=True)
        tools = {n: shutil.which(n) for n in ('cl.exe', 'lib.exe', 'cmake', 'ninja')}
        self.assertTrue(all(tools.values()), str(tools))
        report = {'fixture_only': True, 'cef_sdk_verified': False, 'tools': tools,
                  'archive_count': COUNT, 'all_symbols_called': True, 'commands': []}
        def record():
            proof.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        record()
        with tempfile.TemporaryDirectory(prefix='cef link scale ', dir=os.environ.get('RUNNER_TEMP')) as folder:
            root = Path(folder)
            def run(args, label, cwd=root):
                args = list(map(str, args))
                p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', timeout=240)
                (logs/(label+'.log')).write_text(p.stdout+p.stderr, encoding='utf-8')
                report['commands'].append({'label': label, 'command': args, 'exit_code': p.returncode})
                record()
                return p
            def ok(args, label, cwd=root):
                p = run(args, label, cwd)
                self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
                return p
            template = root/'template.c'
            template.write_text('int cef_rsp_00000(void) { return 1; }\n')
            ok([tools['cl.exe'], '/nologo', '/c', '/MT', '/Od', '/GS-', '/W4', '/WX', template,
                '/Fo'+str(root/'template.obj')], 'compile-template')
            ok([tools['lib.exe'], '/nologo', '/OUT:'+str(root/'template.lib'), root/'template.obj'], 'archive-template')
            raw = (root/'template.lib').read_bytes()
            self.assertTrue(raw.startswith(b'!<arch>\n'))
            self.assertGreaterEqual(raw.count(b'cef_rsp_00000'), 2)
            libs = names()
            producer = root/'producer SDK with spaces'; library_dir = producer/'lib/cef-static'
            library_dir.mkdir(parents=True)
            # Equal-length renaming updates the COFF symbol and archive indexes
            # without moving any offsets. Native link calls EVERY renamed symbol.
            for i, name in enumerate(libs):
                data = raw.replace(b'cef_rsp_00000', f'cef_rsp_{i:05d}'.encode())
                (library_dir/name).write_bytes(data)
            forced = root/'forced.c'
            forced.write_text('extern int forced_value;\n'
                              'static void __cdecl initialize(void) { forced_value = 42; }\n'
                              '#pragma section(".CRT$XCU", read)\n'
                              '__declspec(allocate(".CRT$XCU")) void (__cdecl *forced_initializer)(void) = initialize;\n')
            ok([tools['cl.exe'], '/nologo', '/c', '/MT', '/Od', '/W4', '/WX', forced,
                '/Fo'+str(root/'forced.obj')], 'compile-forced')
            ok([tools['lib.exe'], '/nologo', '/OUT:'+str(library_dir/'cef_objects.lib'), root/'forced.obj'], 'archive-forced')
            cmake_dir = producer/'share/cef-static'; cmake_dir.mkdir(parents=True)
            (cmake_dir/'cef-static-config.cmake').write_text(config(libs), encoding='utf-8')
            sdk = root/'relocated SDK with spaces'; producer.rename(sdk)
            self.assertFalse(producer.exists())
            project = root/'consumer'; project.mkdir()
            (project/'main.c').write_text('#if !defined(_MT) || defined(_DLL)\n#error Static CRT required\n#endif\n'
                'int forced_value;\n'+''.join(f'int cef_rsp_{i:05d}(void);\n' for i in range(COUNT))+
                'int main(void) { int sum = 0;\n'+''.join(f'sum += cef_rsp_{i:05d}();\n' for i in range(COUNT))+
                f'return sum == {COUNT} && forced_value == 42 ? 0 : 1; }}\n')
            (project/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.24)\n'
                'project(link_scale LANGUAGES C)\nfind_package(cef-static CONFIG REQUIRED)\n'
                'add_library(transitive INTERFACE)\ntarget_link_libraries(transitive INTERFACE CEF::static)\n'
                'add_executable(probe main.c)\nset_property(TARGET probe PROPERTY MSVC_RUNTIME_LIBRARY MultiThreaded)\n'
                'target_compile_options(probe PRIVATE /W4 /WX)\ntarget_link_libraries(probe PRIVATE transitive)\n')
            def configure(build, generator, label):
                command = [tools['cmake'], '-S', project, '-B', build, '-G', generator,
                           '-DCMAKE_PREFIX_PATH='+str(sdk), '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF',
                           '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF']
                command += ['-A', 'x64'] if generator.startswith('Visual') else ['-DCMAKE_BUILD_TYPE=Release']
                return run(command, label)
            def build(where, label):
                return run([tools['cmake'], '--build', where, '--config', 'Release'], label)
            # Exact old representation must fail with LNK1170, not an arbitrary error.
            config_file = sdk/'share/cef-static/cef-static-config.cmake'
            config_file.write_text(config(libs, legacy=True), encoding='utf-8')
            old = root/'legacy'; p = configure(old, 'Visual Studio 17 2022', 'legacy-configure')
            self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
            p = build(old, 'legacy-build')
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('LNK1170', p.stdout+p.stderr)
            report['legacy_lnk1170_reproduced'] = True; record()
            config_file.write_text(config(libs), encoding='utf-8')
            for generator, label in [('Visual Studio 17 2022', 'msbuild'), ('Ninja', 'ninja')]:
                where = root/label
                p = configure(where, generator, label+'-configure'); self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
                p = build(where, label+'-build'); self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
                exe = where/'Release/probe.exe' if label == 'msbuild' else where/'probe.exe'
                if label == 'msbuild':
                    tree = ET.parse(where/'probe.vcxproj'); ns = {'m':'http://schemas.microsoft.com/developer/msbuild/2003'}
                    for element in tree.findall('.//m:Link/m:AdditionalDependencies', ns):
                        actual = [s for s in (element.text or '').split(';') if s.startswith('cef_')]
                        self.assertEqual(actual, libs)
                        self.assertLess(len(element.text or ''), 80*1024)
                    report['msbuild_archive_names_and_order_verified'] = True
                hidden = root/'sdk hidden during execution'; sdk.rename(hidden)
                try: ok([exe], label+'-run-sdk-hidden')
                finally: hidden.rename(sdk)
                before = exe.stat().st_mtime_ns
                p = build(where, label+'-noop'); self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
                self.assertEqual(exe.stat().st_mtime_ns, before)
                # Force changed library bytes (not only its timestamp) to require relinking.
                changed = root/'changed.c'
                changed.write_text('int cef_rsp_00000(void) { return 2; }\n')
                time.sleep(1.1)
                ok([tools['cl.exe'], '/nologo', '/c', '/MT', '/Od', '/GS-', changed,
                    '/Fo'+str(root/'changed.obj')], label+'-compile-change')
                path = sdk/'lib/cef-static'/libs[0]; original = path.read_bytes(); path.unlink()
                ok([tools['lib.exe'], '/nologo', '/OUT:'+str(path), root/'changed.obj'], label+'-change-library')
                p = build(where, label+'-relink'); self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
                self.assertGreater(exe.stat().st_mtime_ns, before)
                self.assertEqual(run([exe], label+'-run-changed').returncode, 1)
                time.sleep(1.1); path.write_bytes(original)
                p = build(where, label+'-restore-link'); self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
                ok([exe], label+'-restored-run')
                report[label+'_link_run_relink_verified'] = True; record()
            missing = sdk/'lib/cef-static'/libs[COUNT//2]; missing.unlink()
            p = configure(root/'missing', 'Visual Studio 17 2022', 'missing-library')
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('Missing cef-static archive', p.stdout+p.stderr)
            report['missing_archive_rejected'] = True
            report['status'] = 'success'; record()


if __name__ == '__main__':
    unittest.main()
