"""Validation tools and real PE fixtures, never a CEF runtime certificate."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('audit_builder', ROOT/'vcpkg/ports/cef-static/source_build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
GOOD_OUTPUT = 'Format: COFF-x86-64\nArch: x86_64\nImport {\n  Name: KERNEL32.dll\n}\n'


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='audit unit spaces ')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work/'download/chromium/src'
        self.logs = self.work/'logs'
        self.updater = self.source/'tools/clang/scripts/update.py'
        self.updater.parent.mkdir(parents=True)
        data = b'# locally mocked installer, not upstream\n'
        self.updater.write_bytes(data)
        blob = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        self.addCleanup(patch.stopall)
        patch.object(build, 'WINDOWS', True).start()
        patch.object(build, 'CLANG_UPDATE_BLOB', blob).start()
        patch.dict(os.environ, {}, clear=True).start()
        self.download = patch.object(build, 'run_network', side_effect=self.install).start()
        self.run = patch.object(build, 'run', return_value='LLVM version 23.0.0git\n').start()

    def install(self, command, cwd, logs, name, **kwargs):
        args = [str(a) for a in command]
        self.assertIn('--package=objdump', args)
        self.assertIn('--host-os=win', args)
        installer = Path(args[3])
        self.assertEqual(installer.read_bytes(), self.updater.read_bytes().replace(b'\r\n', b'\n'))
        self.assertFalse(installer.is_relative_to(self.source))
        directory = Path(next(a.split('=', 1)[1] for a in args if a.startswith('--output-dir=')))
        self.assertFalse(directory.is_relative_to(self.source))
        (directory/'bin').mkdir(parents=True)
        (directory/'bin/llvm-readobj.exe').write_bytes(b'MZ-mocked-auditor')
        (directory/'objdump_revision').write_text(build.WINDOWS_AUDIT_VERSION+'\n')
        return ''

    def acquire(self):
        return build.ensure_windows_readobj(self.source, self.work, self.logs)

    def test_install_and_cache_do_not_modify_compiler_or_source(self):
        compiler = self.source/'third_party/llvm-build/Release+Asserts/bin/clang-cl.exe'
        compiler.parent.mkdir(parents=True); compiler.write_bytes(b'compiler sentinel')
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.source.rglob('*') if p.is_file()}
        binary = self.acquire()
        self.assertTrue(binary.is_file()); self.assertFalse(binary.is_relative_to(self.source))
        self.assertEqual(self.acquire(), binary)
        self.download.assert_called_once()
        self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.source.rglob('*') if p.is_file()})
        proof = json.loads((self.logs/'windows-audit-tool.json').read_text())
        self.assertFalse(proof['engine_runtime_verified'])

    def test_crlf_upstream_script_is_normalized_before_blob_check(self):
        self.updater.write_bytes(self.updater.read_bytes().replace(b'\n', b'\r\n'))
        self.acquire(); self.download.assert_called_once()

    def test_wrong_upstream_blob_rejected_without_download(self):
        self.updater.write_text('changed script')
        with self.assertRaisesRegex(RuntimeError, 'Git blob'): self.acquire()
        self.download.assert_not_called()

    def test_unreviewed_origin_rejected_without_download(self):
        with patch.dict(os.environ, CDS_CLANG_BUCKET_OVERRIDE='https://example.invalid'):
            with self.assertRaisesRegex(RuntimeError, 'origin'): self.acquire()
        self.download.assert_not_called()

    def test_failed_download_never_installs_partial_cache(self):
        self.download.side_effect = RuntimeError('simulated transport error')
        with self.assertRaisesRegex(RuntimeError, 'transport'): self.acquire()
        self.assertFalse(list(self.work.glob('windows-audit-*')))
        self.assertFalse(list(self.work.glob('cef-audit-acquire-*')))

    def test_missing_executable_in_download_fails_closed(self):
        self.download.side_effect = lambda *a, **kw: ''
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'): self.acquire()
        self.assertFalse(list(self.work.glob('windows-audit-*')))

    def test_wrong_executable_version_rejected(self):
        self.run.return_value = 'LLVM version 99.0\n'
        with self.assertRaisesRegex(RuntimeError, 'version'): self.acquire()

    def test_modified_cached_binary_rejected_without_redownload(self):
        binary = self.acquire(); binary.write_bytes(b'MZ-replaced')
        with self.assertRaisesRegex(RuntimeError, 'hash mismatch'): self.acquire()
        self.download.assert_called_once()

    def test_stamp_or_receipt_mismatch_is_not_a_cache_hit(self):
        binary = self.acquire(); (binary.parent.parent/'objdump_revision').write_text('wrong')
        with self.assertRaisesRegex(RuntimeError, 'stamp'): self.acquire()
        self.download.assert_called_once()

    def test_existing_unmanaged_directory_is_not_overwritten(self):
        target = self.work/('windows-audit-'+build.WINDOWS_AUDIT_VERSION)
        target.mkdir(); (target/'unrelated').write_text('retain')
        with self.assertRaises(RuntimeError): self.acquire()
        self.assertEqual((target/'unrelated').read_text(), 'retain')
        self.download.assert_not_called()

    def test_no_path_fallback_or_cross_host(self):
        with patch.object(build, 'WINDOWS', False), patch.object(build.shutil, 'which') as which:
            with self.assertRaisesRegex(RuntimeError, 'native Windows'): self.acquire()
            which.assert_not_called()


class BinaryAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root/'fixture.exe'; self.exe.write_bytes(b'MZ fixture')
        self.auditor = self.root/'llvm-readobj.exe'; self.auditor.write_bytes(b'MZ auditor')
        self.addCleanup(patch.stopall)
        self.ensure = patch.object(build, 'ensure_windows_readobj', return_value=self.auditor).start()
        self.run = patch.object(build, 'run', return_value=GOOD_OUTPUT).start()

    def audit(self):
        return build.audit_windows_binary(self.root, self.root, self.root, self.exe)

    def test_real_headers_required_and_no_runtime_success_claim(self):
        self.audit()
        self.assertIn('--coff-imports', self.run.call_args.args[0])
        proof = json.loads((self.root/'binary-imports-proof.json').read_text())
        self.assertTrue(proof['imports_verified']); self.assertFalse(proof['engine_runtime_verified'])

    def test_empty_garbled_and_non_x64_output_rejected(self):
        for text in ['', 'tool ran', GOOD_OUTPUT.replace('COFF-x86-64', 'COFF-i386'),
                     GOOD_OUTPUT.split('Import')[0]]:
            with self.subTest(text=text):
                self.run.return_value = text
                with self.assertRaisesRegex(RuntimeError, 'PE headers'): self.audit()

    def test_direct_and_delay_forbidden_imports_rejected(self):
        for kind in ['Import', 'DelayImport']:
            for dll in ['libcef.dll', 'dxcompiler.dll', 'libGLESv2.dll', 'VCRUNTIME140.dll', 'ucrtbase.dll']:
                self.run.return_value = GOOD_OUTPUT+f'{kind} {{\n  Name: {dll}\n}}\n'
                with self.subTest(kind=kind, dll=dll), self.assertRaisesRegex(RuntimeError, 'forbidden'):
                    self.audit()

    def test_tool_error_and_stale_proof_do_not_certify_binary(self):
        self.audit()
        self.run.side_effect = RuntimeError('tool error')
        with self.assertRaisesRegex(RuntimeError, 'tool error'): self.audit()
        self.assertFalse((self.root/'binary-imports-proof.json').exists())

    def test_acquisition_failure_precedes_compilation(self):
        self.ensure.side_effect = RuntimeError('no auditor')
        with patch.object(build, 'WINDOWS', True), patch.object(build, 'configuration') as config:
            with self.assertRaisesRegex(RuntimeError, 'no auditor'):
                build.compile_and_test(self.root, self.root, self.root, 1)
            config.assert_not_called()
        self.run.assert_not_called()


@unittest.skipUnless(os.name == 'nt' and os.environ.get('CEF_PINNED_CLANG_UPDATE'),
                     'Pinned native Windows auditor is exercised by windows-audit-regression CI')
class NativeWindowsTests(unittest.TestCase):
    def test_pinned_acquisition_and_real_normal_delay_crt_imports(self):
        with tempfile.TemporaryDirectory(prefix='CEF native audit spaces ') as tmp:
            root = Path(tmp); source = root/'source'; logs = root/'logs'; logs.mkdir()
            updater = source/'tools/clang/scripts/update.py'; updater.parent.mkdir(parents=True)
            shutil.copy2(os.environ['CEF_PINNED_CLANG_UPDATE'], updater)
            compiler_sentinel = source/'third_party/llvm-build/Release+Asserts/bin/clang-cl.exe'
            compiler_sentinel.parent.mkdir(parents=True); compiler_sentinel.write_bytes(b'unchanged sentinel')
            before = compiler_sentinel.stat().st_mtime_ns
            readobj = build.ensure_windows_readobj(source, root, logs)
            with patch.object(build, 'run_network', side_effect=AssertionError('cache must not download')):
                self.assertEqual(build.ensure_windows_readobj(source, root, logs), readobj)
            self.assertEqual(compiler_sentinel.read_bytes(), b'unchanged sentinel')
            self.assertEqual(compiler_sentinel.stat().st_mtime_ns, before)
            def compile_file(name, text, *args):
                path = root/(name+'.c'); path.write_text(text)
                result = subprocess.run(['cl.exe', '/nologo', '/W4', '/WX', str(path), *args],
                                        cwd=root, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            compile_file('good', '#include <windows.h>\nint main(void){return GetCurrentProcessId()==0;}\n', '/MT', '/Fe:good.exe')
            build.audit_windows_binary(source, root, logs, root/'good.exe', 'good')
            subprocess.run([root/'good.exe'], check=True, timeout=10)
            compile_file('shared', '__declspec(dllexport) int probe(void){return 42;}\n', '/MT', '/LD', '/Fe:libcef.dll')
            client = '__declspec(dllimport) int probe(void);\nint main(void){return probe()==42?0:1;}\n'
            compile_file('direct', client, '/MT', '/Fe:direct.exe', 'libcef.lib')
            compile_file('delayed', client, '/MT', '/Fe:delayed.exe', 'libcef.lib', 'delayimp.lib', '/link', '/DELAYLOAD:libcef.dll')
            for name in ['direct', 'delayed']:
                with self.assertRaisesRegex(RuntimeError, 'libcef.dll'):
                    build.audit_windows_binary(source, root, logs, root/(name+'.exe'), name)
            self.assertIn('DelayImport {', (logs/'delayed.log').read_text())
            compile_file('dynamic_crt', '#include <stdio.h>\nint main(void){return puts("fixture")==EOF;}\n', '/MD', '/Fe:dynamic_crt.exe')
            with self.assertRaisesRegex(RuntimeError, 'forbidden'):
                build.audit_windows_binary(source, root, logs, root/'dynamic_crt.exe', 'dynamic-crt')
            (root/'truncated.exe').write_bytes(b'MZbad')
            with self.assertRaises(RuntimeError):
                build.audit_windows_binary(source, root, logs, root/'truncated.exe', 'invalid-pe')
            output = ROOT/'static-diagnostics/windows-audit-native'
            output.mkdir(parents=True, exist_ok=True)
            for path in logs.iterdir():
                if path.is_file(): shutil.copy2(path, output/path.name)
            (output/'result.json').write_text(json.dumps({
                'fixture_not_cef': True, 'pinned_auditor_downloaded_and_executed': True,
                'cache_reuse_verified': True, 'compiler_unchanged': True,
                'normal_os_imports_accepted_and_executable_ran': True,
                'normal_and_delay_libcef_imports_rejected': True,
                'dynamic_crt_rejected': True, 'truncated_pe_rejected': True,
            }, indent=2)+'\n')


if __name__ == '__main__': unittest.main()
