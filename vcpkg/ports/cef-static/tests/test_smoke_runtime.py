"""Runtime supervisor contracts; Python children below are NOT CEF certificates."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location('smoke_runtime_tested', ROOT/'vcpkg/ports/cef-static/smoke_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def terminate(process):
    if process.poll() is None:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def proof():
    return {'cef': runtime.VERSION, 'engine': 'static', 'javascript': True, 'paint': True,
            'browser_modules_clean': True, 'renderer_modules_clean': True,
            'browser_pid': 123, 'renderer_pid': 456, 'fixture_not_cef': True}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='smoke runner tests ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root/'work'; self.work.mkdir()
        self.logs = self.root/'logs'

    def run_child(self, script, **kwargs):
        return runtime.run_once([sys.executable, '-u', '-c', script], self.work, self.logs,
                                windows=os.name == 'nt', terminate=terminate, **kwargs)

    def test_real_output_and_zero_exit_preserved(self):
        self.run_child('print("NATIVE_SUPERVISOR_FIXTURE_NOT_CEF")', timeout=10)
        data = json.loads((self.logs/'static-smoke-status.json').read_text())
        self.assertEqual((data['status'], data['exit_code']), ('success', 0))
        self.assertFalse(data['engine_runtime_verified'])
        self.assertIn('NATIVE_SUPERVISOR_FIXTURE_NOT_CEF', (self.logs/'static-smoke.log').read_text())

    def test_nonzero_exit_not_retried_or_certified(self):
        with self.assertRaisesRegex(RuntimeError, 'exited 7'):
            self.run_child('print("failure fixture");raise SystemExit(7)', timeout=10)
        data = json.loads((self.logs/'static-smoke-status.json').read_text())
        self.assertEqual(data['status'], 'failed'); self.assertEqual(data['exit_code'], 7)

    def test_timeout_captures_alive_child_before_kill(self):
        called = []
        def capture(pid, logs, windows):
            called.append(pid)
            # Prove that a successful kill has not already happened.
            if os.name != 'nt': os.kill(pid, 0)
            runtime.write_json(logs/'timeout-processes.json', {'root_pid': pid, 'diagnostic_only': True})
        with patch.object(runtime, 'capture_process', side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                self.run_child('import time;print("before timeout");time.sleep(30)', timeout=1)
        data = json.loads((self.logs/'static-smoke-status.json').read_text())
        self.assertEqual(called, [data['pid']]); self.assertEqual(data['status'], 'timed_out')
        self.assertIsNotNone(data['exit_code']); self.assertNotEqual(data['exit_code'], 0)
        self.assertIn('before timeout', (self.logs/'static-smoke.log').read_text())

    def test_capture_failure_does_not_prevent_kill_or_mask_timeout(self):
        with patch.object(runtime, 'capture_process', side_effect=OSError('capture unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                self.run_child('import time;time.sleep(30)', timeout=1)
        self.assertTrue((self.logs/'timeout-capture-error.json').is_file())
        data = json.loads((self.logs/'static-smoke-status.json').read_text())
        self.assertEqual(data['status'], 'timed_out'); self.assertNotEqual(data['exit_code'], 0)

    def test_native_process_snapshot_for_live_python_child(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'],
                                 start_new_session=os.name != 'nt')
        try:
            runtime.capture_process(child.pid, self.root, os.name == 'nt')
            data = json.loads((self.root/'timeout-processes.json').read_text())
            self.assertEqual(data['root_pid'], child.pid)
            row = next(p for p in data['processes'] if p['pid'] == child.pid)
            self.assertTrue(row['threads']); self.assertTrue(data['diagnostic_only'])
            self.assertNotIn('CommandLine', json.dumps(data))
        finally:
            terminate(child)

    def test_spawn_error_leaves_status(self):
        with self.assertRaises(OSError):
            runtime.run_once([self.work/'missing'], self.work, self.logs,
                             windows=os.name == 'nt', terminate=terminate)
        self.assertEqual(json.loads((self.logs/'static-smoke-status.json').read_text())['status'], 'spawn_failed')


class SmokeProofTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='smoke proof tests ')
        self.addCleanup(temp.cleanup); self.root = Path(temp.name)
        self.exe = self.root/'application.exe'; self.exe.write_bytes(b'fixture - never executed')
        self.logs = self.root/'logs'; self.logs.mkdir()
        self.calls = []

    def fake_run(self, command, cwd, logs, **kwargs):
        self.calls.append((command, cwd, logs, kwargs))
        self.assertFalse((cwd/'test-cache').exists())
        (cwd/'test-cache').mkdir()
        runtime.write_json(cwd/'smoke-result.json', proof())
        (cwd/'cef-static.log').write_text('fixture chromium log')
        (logs/'static-smoke.log').write_text('fixture stdout')
        runtime.write_json(logs/'static-smoke-status.json', {'status': 'success', 'exit_code': 0})

    def execute(self, **kwargs):
        return runtime.execute(self.exe, self.logs, windows=kwargs.get('windows', True), terminate=terminate)

    def test_windows_requires_three_distinct_fresh_profiles(self):
        with patch.object(runtime, 'run_once', side_effect=self.fake_run): result = self.execute()
        self.assertEqual(result, proof()); self.assertEqual(len(self.calls), 3)
        self.assertEqual(len({c[1] for c in self.calls}), 3)
        for command, cwd, logs, kwargs in self.calls:
            self.assertNotEqual(cwd, self.exe.parent); self.assertFalse(cwd.exists())
            self.assertIn('--enable-logging', command)
            self.assertIn('--log-file='+str(cwd/'cef-static.log'), command)
            self.assertTrue((logs/'candidate-smoke-result.json').is_file())
        report = json.loads((self.logs/'smoke-runs.json').read_text())
        self.assertEqual((report['passed_runs'], report['required_runs']), (3, 3))
        self.assertTrue(report['engine_runtime_verified']); self.assertEqual(report['status'], 'success')

    def test_linux_retains_xvfb_and_one_run(self):
        with patch.object(runtime, 'run_once', side_effect=self.fake_run): self.execute(windows=False)
        self.assertEqual(len(self.calls), 1); self.assertEqual(self.calls[0][0][0], 'xvfb-run')

    def test_second_failure_keeps_all_evidence_and_no_stale_success(self):
        runtime.write_json(self.logs/'smoke-result.json', proof())
        def run(*args, **kwargs):
            self.fake_run(*args, **kwargs)
            if len(self.calls) == 2: raise RuntimeError('hung fixture')
        with patch.object(runtime, 'run_once', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'hung fixture'): self.execute()
        self.assertEqual(len(self.calls), 2); self.assertFalse((self.logs/'smoke-result.json').exists())
        report = json.loads((self.logs/'smoke-runs.json').read_text())
        self.assertEqual(report['passed_runs'], 1); self.assertFalse(report['engine_runtime_verified'])
        self.assertEqual(report['status'], 'failed')
        for call in self.calls: self.assertTrue((call[2]/'candidate-smoke-result.json').is_file())

    def test_exit_zero_with_missing_proof_is_failure(self):
        with patch.object(runtime, 'run_once') as run:
            with self.assertRaises(FileNotFoundError): self.execute()
        self.assertEqual(run.call_count, 1); self.assertFalse((self.logs/'smoke-result.json').exists())

    def test_changed_executable_not_certified(self):
        def run(*args, **kwargs):
            self.fake_run(*args, **kwargs); self.exe.write_bytes(b'changed')
        with patch.object(runtime, 'run_once', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'changed'): self.execute()
        self.assertEqual(len(self.calls), 1); self.assertFalse((self.logs/'smoke-result.json').exists())

    def test_incomplete_booleans_and_invalid_pids_rejected(self):
        for key, value in [('javascript', False), ('paint', False), ('browser_modules_clean', False),
                           ('renderer_modules_clean', False), ('browser_pid', True),
                           ('renderer_pid', 0), ('renderer_pid', 123), ('cef', 'wrong'), ('engine', 'shared')]:
            data = proof(); data[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError): runtime.validate_proof(data)

    def test_new_call_cannot_reuse_old_profile_or_proof(self):
        with patch.object(runtime, 'run_once', side_effect=self.fake_run):
            self.execute(windows=False); self.execute(windows=False)
        self.assertNotEqual(self.calls[0][1], self.calls[1][1])
        self.assertNotEqual(self.calls[0][2], self.calls[1][2])

    def test_embeddable_python_import_uses_explicit_sibling_path(self):
        spec = importlib.util.spec_from_file_location('source_build_for_smoke', ROOT/'vcpkg/ports/cef-static/source_build.py')
        build = importlib.util.module_from_spec(spec); spec.loader.exec_module(build)
        with patch.object(build.importlib.util, 'spec_from_file_location', wraps=importlib.util.spec_from_file_location) as load:
            with self.assertRaises(FileNotFoundError): build.execute_smoke(self.root/'absent', self.logs)
            self.assertEqual(load.call_args.args[1], ROOT/'vcpkg/ports/cef-static/smoke_runtime.py')


if __name__ == '__main__': unittest.main()
