"""Launcher and transport regressions; no engine success is inferred here."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PORT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('source_tools_builder',PORT/'source_build.py')
builder=importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)

class SourceToolsTests(unittest.TestCase):
    def test_python_sibling_import_in_isolated_interpreter(self):
        with tempfile.TemporaryDirectory(prefix='cef isolated python ') as directory:
            root=Path(directory)
            (root/'sibling.py').write_text('VALUE=42\n')
            script=root/'probe.py'
            script.write_text('import sibling,sys\nassert sibling.VALUE==42\n'
                              'assert sys.argv[1]=="with spaces"\nprint("OK")\n')
            command=builder.python_script(script,'with spaces')
            # -I is a portable approximation of the embeddable _pth isolation.
            result=subprocess.run([command[0],'-I',*map(str,command[1:])],
                cwd=root.parent,text=True,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(result.stdout.strip(),'OK')

    def test_windows_requires_exe_not_bat(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            batch=root/'git.bat';batch.write_text('@echo fake\n')
            exe=root/'git.exe';exe.write_bytes(b'not executed by this path test')
            with patch.object(builder,'WINDOWS',True):
                with patch.dict(os.environ,{'CEF_STATIC_GIT':str(batch)}):
                    with self.assertRaises(RuntimeError): builder.git_program()
                with patch.dict(os.environ,{'CEF_STATIC_GIT':str(exe)}):
                    self.assertEqual(builder.git_program(),str(exe.resolve()))

    def test_windows_searches_native_exe_explicitly(self):
        with patch.dict(os.environ,{},clear=True), patch.object(builder,'WINDOWS',True), \
             patch.object(builder.shutil,'which',return_value=None) as which:
            with self.assertRaisesRegex(RuntimeError,'Native Git executable'): builder.git_program()
            which.assert_called_once_with('git.exe')

    def test_relative_configured_git_is_rejected(self):
        with patch.dict(os.environ,{'CEF_STATIC_GIT':'git'}):
            with self.assertRaises(RuntimeError): builder.git_program()

    def test_network_retries_known_reset_and_preserves_attempt_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            calls=[]
            def fake_run(command,cwd,logs,name,**kwargs):
                calls.append(name)
                if len(calls)<3:
                    (logs/(name+'.log')).write_text('curl: (35) Recv failure: Connection reset by peer\n')
                    raise RuntimeError('command failed')
                return 'download completed'
            with patch.object(builder,'run',side_effect=fake_run), \
                 patch.object(builder.time,'sleep') as sleep:
                result=builder.run_network(['gclient','runhooks'],root,root,'hooks')
                self.assertEqual(result,'download completed')
                self.assertEqual(calls,['hooks-01','hooks-02','hooks-03'])
                self.assertEqual(sleep.call_count,2)
                self.assertTrue((root/'hooks-01.log').exists())

    def test_non_network_errors_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def fake_run(command,cwd,logs,name,**kwargs):
                (logs/(name+'.log')).write_text('fatal: source SHA mismatch\n')
                raise RuntimeError('pin mismatch')
            with patch.object(builder,'run',side_effect=fake_run) as run, \
                 patch.object(builder.time,'sleep') as sleep:
                with self.assertRaisesRegex(RuntimeError,'pin mismatch'):
                    builder.run_network(['gclient'],root,root,'hooks')
                self.assertEqual(run.call_count,1)
                sleep.assert_not_called()

    def test_retry_limit_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def fake_run(command,cwd,logs,name,**kwargs):
                (logs/(name+'.log')).write_text('HTTP Error 503\n')
                raise RuntimeError('network down')
            with patch.object(builder,'run',side_effect=fake_run) as run, \
                 patch.object(builder.time,'sleep'):
                with self.assertRaises(RuntimeError):
                    builder.run_network(['gclient'],root,root,'sync')
                self.assertEqual(run.call_count,3)
            with self.assertRaises(ValueError):
                builder.run_network([],root,root,'no',attempts=4)

    def test_nontransport_errors_not_classified(self):
        for message in ('error: invalid C++', 'Hash mismatch', 'certificate verify failed',
                        'HTTP Error 404', 'Permission denied', 'unresolved external symbol'):
            with self.subTest(message=message):
                self.assertFalse(builder.transient_network_failure(message))

if __name__=='__main__': unittest.main()
