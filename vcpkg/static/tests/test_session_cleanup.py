#!/usr/bin/env python3
"""Real filesystem regression for the post-SDK Windows WinError 5 failure."""
from __future__ import annotations
import ast
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
STATIC = ROOT/'vcpkg/static'
spec = importlib.util.spec_from_file_location('session_cleanup', STATIC/'session_cleanup.py')
cleanup = importlib.util.module_from_spec(spec); spec.loader.exec_module(cleanup)
LICENSE = 'installed/x64-windows-static/share/cef-static/licenses/third_party/apache-windows-arm64/manual/LICENSE'


def denied(code=5):
    error = PermissionError(errno.EACCES, 'native failure fixture')
    error.winerror = code
    return error


def fingerprint(path):
    info = path.stat()
    return (hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(info.st_mode), info.st_mtime_ns)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cef cleanup fixture ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root/'work'; self.work.mkdir()
        self.session = Path(tempfile.mkdtemp(prefix='cef-sdk-test-', dir=self.work))
        self.logs = self.root/'diagnostics'; self.logs.mkdir()
        self.other = self.work/'download'; self.other.mkdir()
        self.source = self.other/'LICENSE'; self.source.write_bytes(b'license bytes must survive\n')
        self.source.chmod(stat.S_IREAD)
        self.addCleanup(lambda: self.source.chmod(stat.S_IREAD | stat.S_IWRITE))
        self.original = fingerprint(self.source)

    def run_cleanup(self):
        return cleanup.remove_session(self.session, self.work, self.logs)

    def report(self):
        return json.loads((self.logs/'sdk-cleanup-status.json').read_text())

    def test_native_readonly_license_and_git_objects_then_strict_cleanup(self):
        sdk = self.session/'relocated-static-sdk'
        target = sdk/LICENSE; target.parent.mkdir(parents=True)
        shutil.copy2(self.source, target)
        git = shutil.which('git')
        self.assertIsNotNone(git, 'Git is required, never silently skip this regression')
        manager = self.session/'vcpkg-manager'; manager.mkdir()
        subprocess.run([git, 'init', manager], check=True, capture_output=True, timeout=30)
        blob = subprocess.run([git, '-C', manager, 'hash-object', '-w', '--stdin'],
                              input=b'actual Git object\n', check=True, capture_output=True, timeout=30).stdout.decode().strip()
        git_object = manager/'.git/objects'/blob[:2]/blob[2:]
        git_object.chmod(stat.S_IREAD)
        self.addCleanup(lambda: git_object.chmod(stat.S_IWRITE) if git_object.exists() else None)
        self.addCleanup(lambda: target.chmod(stat.S_IWRITE) if target.exists() else None)
        # A minimal already-verified output remains outside cleanup's scope.
        archive = self.root/'static-artifacts/verified.zip'; archive.parent.mkdir()
        import zipfile
        with zipfile.ZipFile(archive, 'w') as output:
            output.write(target, 'sdk/LICENSE')
        archived = fingerprint(archive)
        sibling = self.work/'cef-sdk-test-other'; sibling.mkdir()
        sentinel = sibling/'keep'; sentinel.write_bytes(b'other invocation')
        old_failure = None
        if os.name == 'nt':
            with self.assertRaises(PermissionError) as caught:
                shutil.rmtree(sdk)
            self.assertEqual(caught.exception.winerror, 5)
            old_failure = {'type': 'PermissionError', 'winerror': 5}
        result = self.run_cleanup()
        self.assertEqual(result['status'], 'removed')
        self.assertFalse(self.session.exists())
        self.assertEqual(fingerprint(self.source), self.original)
        self.assertEqual(fingerprint(archive), archived)
        self.assertEqual(sentinel.read_bytes(), b'other invocation')
        if os.name == 'nt':
            recovered = {e['path'] for e in result['readonly_retries'] if e['recovered']}
            self.assertIn('relocated-static-sdk/'+LICENSE, recovered)
            self.assertIn('vcpkg-manager/.git/objects/'+blob[:2]+'/'+blob[2:], recovered)
        else:
            self.assertEqual(result['readonly_retries'], [])
        folder = ROOT/'static-diagnostics/session-cleanup-regression'
        folder.mkdir(parents=True, exist_ok=True)
        (folder/'native.json').write_text(json.dumps({'fixture_only': True,
            'engine_runtime_verified': False, 'platform': sys.platform,
            'python': sys.version, 'legacy_failure': old_failure, 'cleanup': result,
            'source_unchanged': True, 'archive_unchanged': True,
            'sibling_session_unchanged': True}, indent=2)+'\n', encoding='utf-8')

    def test_wrong_roots_are_not_deleted(self):
        candidates = [self.work, self.other, self.root, self.root/'cef-sdk-test-outside',
                      self.work/'cef-sdk-test-', self.session/'nested']
        for path in candidates:
            path.mkdir(exist_ok=True)
            with self.subTest(path=path), self.assertRaises(ValueError):
                cleanup.remove_session(path, self.work, self.logs)
            self.assertTrue(path.is_dir())
            self.assertEqual(self.report()['status'], 'failed')
        self.assertEqual(fingerprint(self.source), self.original)

    def test_missing_root_is_not_silent_success(self):
        self.session.rmdir()
        with self.assertRaises(FileNotFoundError): self.run_cleanup()
        self.assertEqual(self.report()['status'], 'failed')

    def test_unknown_deletion_error_propagates_and_records_failure(self):
        error = OSError(errno.EIO, 'fixture I/O failure')
        with mock.patch.object(cleanup.shutil, 'rmtree', side_effect=error):
            with self.assertRaises(OSError) as caught: self.run_cleanup()
        self.assertIs(caught.exception, error)
        self.assertEqual(self.report()['status'], 'failed')
        self.assertTrue(self.session.exists())
        self.assertEqual(fingerprint(self.source), self.original)

    def test_callback_rejects_wrong_error_operation_and_platform(self):
        for windows, function, error in [(False, os.unlink, denied()),
                (True, os.unlink, denied(32)), (True, os.rmdir, denied()),
                (True, os.scandir, denied()), (True, os.unlink, OSError(errno.EIO, 'I/O'))]:
            with self.subTest(windows=windows, function=function, error=error):
                with mock.patch.object(cleanup, 'WINDOWS', windows), mock.patch.object(cleanup.os, 'chmod') as chmod:
                    with self.assertRaises(type(error)) as caught:
                        cleanup.retry_readonly(function, str(self.source), error, self.session, [])
                    self.assertIs(caught.exception, error); chmod.assert_not_called()

    def test_callback_never_chmods_outside_or_root(self):
        with mock.patch.object(cleanup, 'WINDOWS', True), mock.patch.object(cleanup.os, 'chmod') as chmod:
            for path in [self.source, self.session]:
                with self.assertRaises(PermissionError):
                    cleanup.retry_readonly(os.unlink, str(path), denied(), self.session, [])
            chmod.assert_not_called()

    def test_writable_access_denied_does_not_trigger_permissions_repair(self):
        target = self.session/'writable'; target.write_bytes(b'keep')
        with mock.patch.object(cleanup, 'WINDOWS', True), mock.patch.object(cleanup.os, 'chmod') as chmod:
            with self.assertRaises(PermissionError):
                cleanup.retry_readonly(os.unlink, str(target), denied(), self.session, [])
            chmod.assert_not_called()

    def test_retry_failure_is_not_swallowed_and_restores_mode(self):
        target = self.session/'LICENSE'; target.write_bytes(b'keep')
        target.chmod(stat.S_IREAD)
        self.addCleanup(lambda: target.chmod(stat.S_IWRITE) if target.exists() else None)
        real = target.lstat()
        info = types.SimpleNamespace(st_mode=real.st_mode, st_dev=real.st_dev,
            st_ino=real.st_ino, st_file_attributes=cleanup.READONLY)
        original_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            return info if path == target else original_lstat(path, *args, **kwargs)
        failure = denied(32); retries = []
        with mock.patch.object(cleanup, 'WINDOWS', True), mock.patch.object(Path, 'lstat', lstat), \
                mock.patch.object(cleanup.os, 'unlink', side_effect=failure) as unlink:
            with self.assertRaises(PermissionError) as caught:
                cleanup.retry_readonly(unlink, str(target), denied(), self.session, retries)
            self.assertIs(caught.exception, failure)
            unlink.assert_called_once_with(str(target))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), stat.S_IMODE(real.st_mode))
        self.assertFalse(retries[0]['recovered'])

    @unittest.skipUnless(os.name == 'nt', 'Requires native NTFS attributes')
    def test_native_owned_readonly_hardlinks_are_removed(self):
        a = self.session/'a'; shutil.copy2(self.source, a)
        b = self.session/'b'; os.link(a, b)
        self.addCleanup(lambda: a.chmod(stat.S_IWRITE) if a.exists() else None)
        self.addCleanup(lambda: b.chmod(stat.S_IWRITE) if b.exists() else None)
        self.assertEqual(a.stat().st_nlink, 2)
        self.assertEqual(self.run_cleanup()['status'], 'removed')
        self.assertEqual(fingerprint(self.source), self.original)

    @unittest.skipUnless(os.name == 'nt', 'Requires native NTFS attributes')
    def test_native_external_readonly_hardlink_is_rejected_before_deletion(self):
        link = self.session/'external-license'; os.link(self.source, link)
        unrelated = self.session/'keep'; unrelated.write_bytes(b'not yet deleted')
        with self.assertRaisesRegex(ValueError, 'not owned exclusively'): self.run_cleanup()
        self.assertTrue(unrelated.exists())
        self.assertEqual(fingerprint(self.source), self.original)
        self.source.chmod(stat.S_IWRITE); link.unlink(); self.source.chmod(stat.S_IREAD)

    @unittest.skipUnless(os.name == 'nt', 'Requires native Win32 handle sharing')
    def test_native_sharing_violation_is_fatal(self):
        import ctypes
        from ctypes import wintypes
        target = self.session/'locked'; target.write_bytes(b'held without FILE_SHARE_DELETE')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle; close.argtypes = [wintypes.HANDLE]; close.restype = wintypes.BOOL
        handle = create(str(target), 0x80000000, 1, None, 3, 0, None)
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        try:
            with self.assertRaises(PermissionError) as caught: self.run_cleanup()
            self.assertEqual(caught.exception.winerror, 32)
            self.assertEqual(self.report()['status'], 'failed')
            self.assertEqual(self.report()['readonly_retries'], [])
        finally:
            self.assertTrue(close(handle))
        self.assertEqual(self.run_cleanup()['status'], 'removed')

    def make_directory_link(self, link):
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(self.other)],
                           check=True, capture_output=True, timeout=30)
        else:
            link.symlink_to(self.other, target_is_directory=True)

    def test_native_directory_link_target_not_traversed(self):
        self.make_directory_link(self.session/'outside-link')
        self.assertEqual(self.run_cleanup()['status'], 'removed')
        self.assertEqual(fingerprint(self.source), self.original)

    def test_native_root_link_is_rejected(self):
        self.session.rmdir(); self.make_directory_link(self.session)
        try:
            with self.assertRaises(ValueError): self.run_cleanup()
            self.assertEqual(fingerprint(self.source), self.original)
        finally:
            self.session.rmdir() if os.name == 'nt' else self.session.unlink()

    def test_callback_rejects_redirected_parent(self):
        link = self.session/'outside-link'; self.make_directory_link(link)
        with mock.patch.object(cleanup, 'WINDOWS', True), mock.patch.object(cleanup.os, 'chmod') as chmod:
            with self.assertRaises(PermissionError):
                cleanup.retry_readonly(os.unlink, str(link/'LICENSE'), denied(), self.session, [])
            chmod.assert_not_called()
        self.run_cleanup()

    def test_ci_cleanup_is_success_only_after_diagnostics_before_success_marker(self):
        tree = ast.parse((STATIC/'ci.py').read_text())
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        guarded = next(node for node in main.body if isinstance(node, ast.Try))
        # Interpret only the outer control-flow skeleton. This verifies that
        # runtime, packaging or log-copy failures cannot reach cleanup/success.
        following = main.body[main.body.index(guarded)+1:]
        self.assertEqual(len(following), 2)
        self.assertEqual(ast.unparse(following[0].value.func), 'session_cleanup.remove_session')
        self.assertIn('STATIC_ENGINE_VCPKG_EXTERNAL_CAPI_CONSUMER_VERIFIED', ast.unparse(following[1]))
        for fail in [None, 'runtime', 'packaging', 'diagnostics', 'cleanup']:
            events = []
            def stage(name):
                events.append(name)
                if name == fail: raise RuntimeError(name)
            simulated = ast.Module(body=[ast.Try(body=[ast.parse("stage('runtime')").body[0],
                ast.parse("stage('packaging')").body[0]], handlers=[], orelse=[],
                finalbody=[ast.parse("stage('diagnostics')").body[0]]), *following], type_ignores=[])
            namespace = {'stage': stage, 'session_cleanup': types.SimpleNamespace(remove_session=lambda *a: stage('cleanup')),
                         'session': None, 'work': None, 'diagnostics': None, 'print': lambda *a, **k: stage('success')}
            if fail:
                with self.assertRaisesRegex(RuntimeError, fail): exec(compile(ast.fix_missing_locations(simulated), '<ci-control-flow>', 'exec'), namespace)
                self.assertNotIn('success', events)
                if fail != 'cleanup': self.assertNotIn('cleanup', events)
            else:
                exec(compile(ast.fix_missing_locations(simulated), '<ci-control-flow>', 'exec'), namespace)
                self.assertEqual(events, ['runtime', 'packaging', 'diagnostics', 'cleanup', 'success'])


if __name__ == '__main__': unittest.main()
