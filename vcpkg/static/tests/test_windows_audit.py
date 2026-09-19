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
GOOD_OUTPUT = 'FILE HEADER VALUES\n 8664 machine (x64)\n 20B magic # (PE32+)\n Section contains the following imports:\n KERNEL32.dll\n'


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='audit unit spaces ')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work/'source'
        self.logs = self.work/'logs'
        self.program_files = self.work/'program files x86'
        self.vswhere = self.program_files/'Microsoft Visual Studio/Installer/vswhere.exe'
        self.vswhere.parent.mkdir(parents=True); self.vswhere.write_bytes(b'MZ finder')
        self.vs = self.work/'VS 2022'
        self.version_file = self.vs/'VC/Auxiliary/Build/Microsoft.VCToolsVersion.default.txt'
        self.version_file.parent.mkdir(parents=True); self.version_file.write_text('14.44.35207')
        self.binary = self.vs/'VC/Tools/MSVC/14.44.35207/bin/Hostx64/x64/dumpbin.exe'
        self.binary.parent.mkdir(parents=True); self.binary.write_bytes(b'MZ auditor')
        self.addCleanup(patch.stopall)
        patch.object(build, 'WINDOWS', True).start()
        patch.dict(os.environ, {'ProgramFiles(x86)':str(self.program_files)}, clear=True).start()
        self.run = patch.object(build, 'run', side_effect=self.invoke).start()

    def invoke(self, command, *args, **kwargs):
        if Path(command[0]) == self.vswhere:
            self.assertIn('[17.0,18.0)', command)
            return str(self.vs)+'\n'
        self.assertEqual(command, [self.binary, '/HEADERS', self.binary])
        return 'Microsoft (R) COFF/PE Dumper Version 14.44.35219.0\n'+GOOD_OUTPUT

    def discover(self):
        return build.ensure_windows_dumpbin(self.source, self.work, self.logs)

    def test_exact_native_tool_and_provenance_without_source_changes(self):
        before={str(p):p.read_bytes() for p in self.work.rglob('*') if p.is_file()}
        self.assertEqual(self.discover(), self.binary)
        for name,data in before.items(): self.assertEqual(Path(name).read_bytes(),data)
        proof=json.loads((self.logs/'windows-audit-tool.json').read_text())
        self.assertEqual(proof['toolset_version'],'14.44.35207')
        self.assertEqual(proof['clang_msc_version'],1944)
        self.assertEqual(proof['executable_sha256'],build.digest(self.binary))
        self.assertFalse(proof['engine_runtime_verified'])

    def test_missing_vswhere_does_not_use_path_fallback(self):
        self.vswhere.unlink()
        with patch.object(build.shutil,'which') as which, self.assertRaises(RuntimeError): self.discover()
        which.assert_not_called();self.run.assert_not_called()

    def test_ambiguous_or_missing_visual_studio_is_rejected(self):
        for value in ['',str(self.vs)+'\n'+str(self.vs), 'relative']:
            self.run.side_effect=None;self.run.return_value=value
            with self.subTest(value=value),self.assertRaises(RuntimeError):self.discover()

    def test_invalid_or_unreviewed_toolset_version_is_rejected(self):
        for value in ['../other','14.44.35207/evil','15.0.12345',
                      '14.43.34808','14.45.12345']:
            self.version_file.write_text(value)
            with self.subTest(value=value),self.assertRaises(RuntimeError):self.discover()

    def test_missing_or_non_pe_auditor_rejected(self):
        self.binary.write_bytes(b'not a PE')
        with self.assertRaises(RuntimeError):self.discover()
        self.binary.unlink()
        with self.assertRaises(RuntimeError):self.discover()

    def test_discovery_failure_removes_stale_proof(self):
        self.discover();self.run.side_effect=RuntimeError('discovery failed')
        with self.assertRaises(RuntimeError):self.discover()
        self.assertFalse((self.logs/'windows-audit-tool.json').exists())

    def test_unrecognized_auditor_version_rejected(self):
        self.run.side_effect=[str(self.vs),'unknown tool']
        with self.assertRaisesRegex(RuntimeError,'version'):self.discover()

    def test_help_exit_is_not_accepted_as_tool_success(self):
        self.run.side_effect = [str(self.vs), RuntimeError('exited 1100')]
        with self.assertRaisesRegex(RuntimeError, '1100'):
            self.discover()
        self.assertFalse((self.logs/'windows-audit-tool.json').exists())

    def test_version_banner_without_decoded_x64_headers_is_rejected(self):
        banner = 'Microsoft (R) COFF/PE Dumper Version 14.44.35219.0\n'
        for output in (banner, banner+GOOD_OUTPUT.replace('8664', '14C')):
            self.run.side_effect = [str(self.vs), output]
            with self.assertRaisesRegex(RuntimeError, 'self-probe'):
                self.discover()
            self.assertFalse((self.logs/'windows-audit-tool.json').exists())

    def test_wrong_host_rejected_before_discovery(self):
        with patch.object(build,'WINDOWS',False),self.assertRaises(RuntimeError):self.discover()
        self.run.assert_not_called()


class BinaryAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root/'fixture.exe'; self.exe.write_bytes(b'MZ fixture')
        self.auditor = self.root/'dumpbin.exe'; self.auditor.write_bytes(b'MZ auditor')
        self.addCleanup(patch.stopall)
        self.ensure = patch.object(build, 'ensure_windows_dumpbin', return_value=self.auditor).start()
        self.run = patch.object(build, 'run', return_value=GOOD_OUTPUT).start()

    def audit(self):
        return build.audit_windows_binary(self.root, self.root, self.root, self.exe)

    def test_real_headers_required_and_no_runtime_success_claim(self):
        self.audit()
        self.assertIn('/IMPORTS', self.run.call_args.args[0])
        proof = json.loads((self.root/'binary-imports-proof.json').read_text())
        self.assertTrue(proof['imports_verified']); self.assertFalse(proof['engine_runtime_verified'])

    def test_empty_garbled_and_non_x64_output_rejected(self):
        for text in ['', 'tool ran', GOOD_OUTPUT.replace('8664 machine (x64)', '14C machine (x86)'),
                     GOOD_OUTPUT.split('Section contains')[0]]:
            with self.subTest(text=text):
                self.run.return_value = text
                with self.assertRaisesRegex(RuntimeError, 'PE headers'): self.audit()

    def test_direct_and_delay_forbidden_imports_rejected(self):
        for kind in ['imports', 'delay load imports']:
            for dll in ['libcef.dll', 'dxcompiler.dll', 'libGLESv2.dll', 'VCRUNTIME140.dll', 'ucrtbase.dll']:
                self.run.return_value = GOOD_OUTPUT+f'Section contains the following {kind}:\n {dll}\n'
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


@unittest.skipUnless(os.name == 'nt' and os.environ.get('CEF_NATIVE_WINDOWS_AUDIT'),
                     'Native Windows auditor is exercised by windows-audit-regression CI')
class NativeWindowsTests(unittest.TestCase):
    def test_native_auditor_and_real_normal_delay_crt_imports(self):
        with tempfile.TemporaryDirectory(prefix='CEF native audit spaces ') as tmp:
            root = Path(tmp); source = root/'source'; logs = root/'logs'; logs.mkdir()
            output = ROOT/'static-diagnostics/windows-audit-native'
            output.mkdir(parents=True, exist_ok=True)
            (output/'result.json').unlink(missing_ok=True)
            try:
                source.mkdir()
                sentinel = source/'compiler-sentinel';sentinel.write_bytes(b'unchanged compiler')
                before=sentinel.stat().st_mtime_ns
                # No developer-prompt PATH is needed to discover or execute the auditor.
                with patch.dict(os.environ, PATH=str(Path(os.environ['SystemRoot'])/'System32')):
                    readobj=build.ensure_windows_dumpbin(source, root, logs)
                self.assertEqual(sentinel.read_bytes(),b'unchanged compiler')
                self.assertEqual(sentinel.stat().st_mtime_ns,before)
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
                self.assertIn('delay load imports', (logs/'delayed.log').read_text().lower())
                compile_file('dynamic_crt', '#include <stdio.h>\nint main(void){return puts("fixture")==EOF;}\n', '/MD', '/Fe:dynamic_crt.exe')
                with self.assertRaisesRegex(RuntimeError, 'forbidden'):
                    build.audit_windows_binary(source, root, logs, root/'dynamic_crt.exe', 'dynamic-crt')
                (root/'truncated.exe').write_bytes(b'MZbad')
                with self.assertRaises(RuntimeError):
                    build.audit_windows_binary(source, root, logs, root/'truncated.exe', 'invalid-pe')
            finally:
                for path in logs.iterdir():
                    if path.is_file(): shutil.copy2(path, output/path.name)
            (output/'result.json').write_text(json.dumps({
                'fixture_not_cef': True, 'native_vs_auditor_discovered_and_executed': True,
                'sanitized_path_verified': True, 'compiler_unchanged': True,
                'normal_os_imports_accepted_and_executable_ran': True,
                'normal_and_delay_libcef_imports_rejected': True,
                'dynamic_crt_rejected': True, 'truncated_pe_rejected': True,
            }, indent=2)+'\n')


if __name__ == '__main__': unittest.main()
