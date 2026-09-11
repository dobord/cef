"""Regression coverage for CI run 34505690797; NOT a CEF engine build test."""
from __future__ import annotations
import contextlib
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PORT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent/'fixtures'

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

build = load('completion_source_build', PORT/'source_build.py')
export = load('completion_export_static', PORT/'export_static.py')


class NativeMetadataTests(unittest.TestCase):
    def test_all_real_linux_flags_are_reviewed(self):
        data = json.loads((FIXTURES/'native-linux.json').read_text())
        kept, omitted = export.link_options(data['ldflags'], False, lambda x: x)
        self.assertEqual(set(data['ldflags']), set(kept) | {x['flag'] for x in omitted})
        self.assertIn('-Wl,-z,defs', kept)
        self.assertIn('-Wl,-z,relro', kept)
        self.assertIn('-Wl,-z,now', kept)

    def test_all_real_windows_flags_are_reviewed(self):
        data = json.loads((FIXTURES/'native-windows.json').read_text())
        kept, omitted = export.link_options(data['ldflags'], True, lambda x: x)
        self.assertEqual(set(data['ldflags']), set(kept) | {x['flag'] for x in omitted})
        self.assertIn('/FIXED:NO', kept)
        self.assertIn('/guard:cf', kept)
        self.assertIn('/DYNAMICBASE', kept)
        self.assertIn('/DEFAULTLIB:libcpmt.lib', kept)
        self.assertIn('/STACK:0x800000', kept)
        self.assertTrue(export.WINDOWS_FLAG_LIBRARIES <= {x.lower() for x in kept})
        self.assertIn('--color-diagnostics', {x['flag'] for x in omitted})

    def test_unknown_windows_lib_is_not_silently_accepted(self):
        with self.assertRaisesRegex(RuntimeError, 'Unreviewed'):
            export.link_options(['unreviewed.lib'], True, lambda x: x)

    def test_unknown_flags_still_fail_on_both_platforms(self):
        for windows, flag in ((True, '/surprise'), (False, '-Wl,--surprise')):
            with self.subTest(windows=windows), self.assertRaises(RuntimeError):
                export.link_options([flag], windows, lambda x: x)

    def test_dxc_cannot_hide_in_lib_flags(self):
        for flag in ('dxcompiler.lib', '/DELAYLOAD:dxcompiler.dll', 'dxil.lib', '/DELAYLOAD:libcef.dll'):
            with self.subTest(flag=flag), self.assertRaises(RuntimeError):
                export.link_options([flag], True, lambda x: x)

    def test_executable_entrypoint_is_not_exported(self):
        with self.assertRaisesRegex(RuntimeError, 'entry point'):
            export.link_options(['/ENTRY:mainCRTStartup'], True, lambda x: x)

    def test_actual_linux_link_edge(self):
        with gzip.open(FIXTURES/'native-link-linux.txt.gz', 'rt') as stream:
            items = export.query_link_inputs(stream.read())
        self.assertEqual(len(items), 23112)
        self.assertIn('obj/cef/cef_static_smoke/smoke.o', items)

    def test_actual_windows_link_edge(self):
        with gzip.open(FIXTURES/'native-link-windows.txt.gz', 'rt') as stream:
            items = export.query_link_inputs(stream.read())
        self.assertEqual(len(items), 23077)
        self.assertIn('obj/cef/cef_static_smoke/smoke.obj', items)

    def test_direct_shared_library_rejected(self):
        for shared in ('libcef.dll.lib', 'libcef.so.TOC', 'other.so', 'other.dll.lib'):
            query = f'app:\n  input: link\n    app.o\n    {shared}\n  outputs:\n'
            with self.subTest(shared=shared), self.assertRaises(RuntimeError):
                export.query_link_inputs(query)

    def test_build_only_shared_stub_is_not_link_input(self):
        query = 'app:\n  input: link\n    app.o\n    || libEGL.so.TOC\n  outputs:\n'
        self.assertEqual(export.query_link_inputs(query), ['app.o'])

    def test_no_success_from_configuration_receipt(self):
        with self.assertRaises(RuntimeError):
            export.verify_reference({'phase': 'graph-only', 'engine_link_verified': False})


class PhaseTests(unittest.TestCase):
    def invoke_phase(self, phase, root):
        argv = ['source_build.py', phase, '--work', str(root/'work'), '--logs', str(root/'logs')]
        with mock.patch.object(sys, 'argv', argv), \
             mock.patch.object(build.platform, 'machine', return_value='x86_64'), \
             mock.patch.object(build, 'setup_environment'), \
             mock.patch.object(build, 'prepare', return_value=root/'source') as prepare, \
             mock.patch.object(build, 'configuration', return_value=root/'out') as configure, \
             mock.patch.object(build, 'compile_regressions') as regress, \
             mock.patch.object(build, 'compile_and_test') as native, \
             contextlib.redirect_stdout(io.StringIO()):
            build.main()
        return prepare, configure, regress, native

    def test_check_never_starts_native_compile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare, configure, regress, native = self.invoke_phase('check', root)
            prepare.assert_called_once()
            configure.assert_called_once()
            regress.assert_not_called()
            native.assert_not_called()
            receipt = json.loads((root/'logs/configuration-receipt.json').read_text())
            self.assertEqual(receipt['phase'], 'graph-only')
            self.assertIs(receipt['engine_link_verified'], False)
            self.assertIs(receipt['engine_runtime_verified'], False)

    def test_regression_compile_requires_explicit_phase(self):
        with tempfile.TemporaryDirectory() as temp:
            _, configure, regress, native = self.invoke_phase('regressions', Path(temp))
            configure.assert_called_once()
            regress.assert_called_once()
            native.assert_not_called()

    def test_build_phase_does_not_skip_native_build_and_run(self):
        with tempfile.TemporaryDirectory() as temp:
            _, configure, regress, native = self.invoke_phase('build', Path(temp))
            configure.assert_not_called()
            regress.assert_not_called()
            native.assert_called_once()

    def test_timeout_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(build.build_timeout(), 18000)

    def test_timeout_configurable_for_persistent_runner(self):
        with mock.patch.dict(os.environ, {'CEF_STATIC_BUILD_TIMEOUT_SECONDS':'86400'}):
            self.assertEqual(build.build_timeout(), 86400)

    def test_invalid_timeouts_rejected(self):
        for value in ('0', '-2', 'abc', '1.2', '172801'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {'CEF_STATIC_BUILD_TIMEOUT_SECONDS':value}):
                with self.assertRaises(ValueError):
                    build.build_timeout()

    def test_zero_jobs_rejected_before_source_access(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(sys, 'argv', ['source_build.py', 'check', '--work', temp, '--logs', temp, '--jobs', '0']), \
             mock.patch.object(build, 'prepare') as prepare, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build.main()
            prepare.assert_not_called()


class ProcessStatusTests(unittest.TestCase):
    def test_success_status_is_saved(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temp)
            output = build.run([sys.executable, '-c', 'print("helper-ok")'], root, root, 'helper', timeout=15)
            data = json.loads((root/'helper-status.json').read_text())
            self.assertIn('helper-ok', output)
            self.assertEqual(data['status'], 'success')
            self.assertEqual(data['exit_code'], 0)

    def test_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temp)
            with self.assertRaisesRegex(RuntimeError, 'exited 7'):
                build.run([sys.executable, '-c', 'raise SystemExit(7)'], root, root, 'failure', timeout=15)
            data = json.loads((root/'failure-status.json').read_text())
            self.assertEqual(data['status'], 'failed')
            self.assertEqual(data['exit_code'], 7)

    def test_timeout_has_explicit_failure_receipt(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temp)
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                build.run([sys.executable, '-c', 'import time; time.sleep(30)'], root, root, 'timeout', timeout=1)
            data = json.loads((root/'timeout-status.json').read_text())
            self.assertEqual(data['status'], 'timed_out')
            self.assertNotEqual(data['exit_code'], 0)
            self.assertLess(data['elapsed_seconds'], 20)


class RelocationTests(unittest.TestCase):
    def test_both_roots_hidden_and_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); paths = [root/'sdk', root/'source']
            for p in paths:
                p.mkdir(); (p/'marker').write_text(p.name)
            with build.hidden_directories(paths):
                self.assertTrue(all(not p.exists() for p in paths))
            for p in paths:
                self.assertEqual((p/'marker').read_text(), p.name)
            self.assertFalse(list(root.glob('.cef-hidden-*')))

    def test_runtime_error_restores_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'sdk'; path.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'runtime failure'):
                with build.hidden_directories([path]):
                    raise RuntimeError('runtime failure')
            self.assertTrue(path.is_dir())

    def test_partial_hide_failure_restores_already_hidden_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sdk = root/'sdk'; sdk.mkdir()
            (sdk/'data').write_text('must survive')
            with self.assertRaises(FileNotFoundError):
                with build.hidden_directories([sdk, root/'missing-source']):
                    self.fail('must not enter runtime test')
            self.assertEqual((sdk/'data').read_text(), 'must survive')
            self.assertFalse(list(root.glob('.cef-hidden-*')))

    def test_restore_failure_preserves_original_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sdk = root/'sdk'; sdk.mkdir(); (sdk/'data').write_text('keep')
            rename = Path.rename
            def fail_restore(path, target):
                if path.name == 'payload':
                    raise PermissionError('simulated restore failure')
                return rename(path, target)
            with mock.patch.object(Path, 'rename', fail_restore), self.assertRaises(PermissionError):
                with build.hidden_directories([sdk]):
                    pass
            saved = list(root.glob('.cef-hidden-*/payload/data'))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_text(), 'keep')


class BinaryAuditTests(unittest.TestCase):
    def test_windows_system_dlls_allowed(self):
        build.verify_binary_imports('Name: KERNEL32.dll\nName: USER32.dll\nName: bcrypt.dll', True)

    def test_linux_system_libraries_allowed(self):
        build.verify_binary_imports('Shared library: [libc.so.6]\nShared library: [libX11.so.6]', False)

    def test_engine_dll_and_so_rejected(self):
        for windows, name in ((True,'libcef.dll'),(False,'libcef.so'),(True,'chrome_elf.dll'),(False,'libGLESv2.so')):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                build.verify_binary_imports(name, windows)

    def test_windows_dynamic_crt_rejected(self):
        for name in ('VCRUNTIME140.dll','VCRUNTIME140_1.dll','MSVCP140.dll','ucrtbase.dll'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                build.verify_binary_imports(name, True)

    def test_dynamic_shader_compiler_rejected(self):
        for name in ('dxcompiler.dll','dxil.dll'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                build.verify_binary_imports(name, True)

    def test_dynamic_media_library_rejected(self):
        for windows, name in ((True,'ffmpeg.dll'), (False,'libffmpeg.so')):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                build.verify_binary_imports(name, windows)


if __name__ == '__main__':
    unittest.main()
