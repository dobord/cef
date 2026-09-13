"""POSIX names must round-trip without weakening checkpoint containment checks."""
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checkpoint as windows
import linux_checkpoint as cp

ID = {'platform': 'linux-x64', 'recipe': 'posix-path-regression', 'schema': cp.SCHEMA}
SYSROOT_NAME = (r'download/chromium/src/build/linux/debian_bullseye_amd64-sysroot/'
                r'lib/systemd/system/system-systemd\x2dcryptsetup.slice')


class PathPolicyTests(unittest.TestCase):
    def test_posix_names_are_returned_verbatim(self):
        for name in (SYSROOT_NAME, r'types\stack-trace', 'lib/pkg:arch.so',
                     r'..\literal', r'\literal', r'C:\literal', 'dir/space name'):
            with self.subTest(name=name):
                self.assertEqual(cp.relative(name), name)

    def test_absolute_traversal_and_ambiguous_members_are_rejected(self):
        for name in ('', '/', '/abs', '//abs', '.', '..', '../escape',
                     'a/../escape', './a', 'a/./b', 'a//b', 'a/', 'a\0b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                cp.relative(name)

    def test_symlink_spelling_is_not_windows_normalized(self):
        for target in (r'types\stack-trace', r'..\literal', 'lib:1',
                       r'C:\literal', './file', 'dir//file', '../file'):
            with self.subTest(target=target):
                self.assertEqual(cp.link_target('sub/alias', target), target)

    def test_symlink_targets_must_stay_inside_workspace(self):
        for target in ('', '/etc/passwd', '//etc/passwd', '../../escape',
                       '../..', '../dir/../../escape', 'a\0b', 'a\nb', 'a\rb'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                cp.link_target('sub/alias', target)

    def test_windows_policy_is_not_monkey_patched(self):
        for name in (r'types\stack-trace', 'lib:1', r'C:\literal'):
            cp.relative(name)
            with self.assertRaises(ValueError):
                windows.relative(name)
        self.assertEqual(windows.link_target('sub/alias', r'..\file'), '../file')
        self.assertEqual(cp.link_target('sub/alias', r'..\file'), r'..\file')


@unittest.skipUnless(sys.platform == 'linux', 'Native Linux pathname semantics required')
class WorkspacePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root/'work'
        self.work.mkdir()
        self.package = self.root/'package'
        (self.work/'object.o').write_bytes(b'fixture-not-CEF')

    def write(self, name, data=b'fixture'):
        path = self.work/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def roundtrip(self):
        counts = cp.preflight(self.work)
        manifest = cp.save(self.work, self.package, ID, limit=97)
        self.assertEqual(counts['files'], manifest['files'])
        self.assertEqual(counts['links'], len(manifest['links']))
        self.assertEqual(counts['directories'], len(manifest['directories']))
        shutil.rmtree(self.work)
        cp.restore(self.package, self.work, ID)
        self.assertEqual(counts, cp.preflight(self.work))
        return manifest

    def test_observed_sysroot_and_node_names_roundtrip_without_aliasing(self):
        names = {SYSROOT_NAME: b'systemd', r'types\stack-trace': b'literal',
                 'types/stack-trace': b'separator', 'lib:amd64/file': b'colon',
                 r'dir\name/payload': b'directory'}
        for name, data in names.items():
            self.write(name, data)
        self.roundtrip()
        for name, data in names.items():
            self.assertEqual((self.work/name).read_bytes(), data)
        self.assertFalse((self.work/SYSROOT_NAME.replace('\\', '/')).exists())

    def test_literal_symlink_name_target_and_clocks_roundtrip(self):
        target = self.write(r'sub/..\literal', b'literal-not-parent')
        target.chmod(0o755)
        timestamp = 1700000000123456789
        os.utime(target, ns=(timestamp, timestamp))
        name, spelling = r'sub/alias\name', r'./..\literal'
        os.symlink(spelling, self.work/name)
        cp.set_link_mtime(self.work/name, timestamp+1)
        manifest = self.roundtrip()
        self.assertEqual(manifest['links'][0]['target'], spelling)
        self.assertEqual(os.readlink(self.work/name), spelling)
        self.assertEqual((self.work/name).read_bytes(), b'literal-not-parent')
        self.assertEqual((self.work/name).lstat().st_mtime_ns, timestamp+1)
        self.assertEqual(target.stat().st_mtime_ns, timestamp)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_dangling_internal_link_keeps_literal_backslash(self):
        os.symlink(r'missing\file', self.work/'alias')
        self.roundtrip()
        self.assertEqual(os.readlink(self.work/'alias'), r'missing\file')
        self.assertFalse((self.work/'alias').exists())

    def test_absolute_and_escaping_symlinks_fail_before_ninja(self):
        for target in ('/etc/passwd', '../outside', 'dir/../../outside'):
            with self.subTest(target=target):
                os.symlink(target, self.work/'alias')
                try:
                    with self.assertRaises(ValueError):
                        cp.preflight(self.work)
                finally:
                    (self.work/'alias').unlink()

    def test_symlink_cycles_are_rejected(self):
        os.symlink('b', self.work/'a')
        os.symlink('a', self.work/'b')
        with self.assertRaises(ValueError):
            cp.preflight(self.work)

    def test_symlink_chain_cannot_escape_via_dotdot(self):
        (self.work/'sub').mkdir()
        os.symlink('..', self.work/'sub/up')
        os.symlink('sub/up/../outside', self.work/'alias')
        with self.assertRaises(ValueError):
            cp.preflight(self.work)

    def test_credentials_and_git_config_still_fail_closed(self):
        for name in (r'folder\name/.netrc', 'folder:arch/.boto',
                     'CREDENTIALS.JSON', '.git/config'):
            with self.subTest(name=name):
                path = self.write(name, b'[http]\nextraheader = SYNTHETIC-NOT-A-TOKEN\n')
                try:
                    with self.assertRaises(ValueError):
                        cp.preflight(self.work)
                finally:
                    path.unlink()

    def test_credential_alias_is_rejected(self):
        secret = self.write(next(iter(cp.OMITTED_FILES)), b'SYNTHETIC-NOT-A-TOKEN')
        os.symlink(secret.relative_to(self.work).as_posix(), self.work/r'alias\name')
        with self.assertRaises(ValueError):
            cp.preflight(self.work)

    def test_special_files_are_rejected(self):
        os.mkfifo(self.work/r'pipe\name')
        with self.assertRaises(ValueError):
            cp.preflight(self.work)

    def test_linux_preflight_rejects_wrong_host(self):
        with patch.object(cp.sys, 'platform', 'win32'), self.assertRaises(ValueError):
            cp.preflight(self.work)

    def test_restore_rejects_unsafe_directory_and_link_names(self):
        manifest = cp.save(self.work, self.package, ID, limit=97)
        shutil.rmtree(self.work)
        for field in ('directories', 'links'):
            for name in ('../escape', '/escape', 'a/../escape', 'a//b'):
                with self.subTest(field=field, name=name):
                    bad = dict(manifest)
                    bad[field] = [name] if field == 'directories' else [
                        {'name': name, 'target': 'object.o', 'directory': False, 'mtime_ns': 0}]
                    (self.package/'checkpoint.json').write_text(json.dumps(bad))
                    with self.assertRaises(ValueError):
                        cp.restore(self.package, self.work, ID)
                    self.assertFalse(self.work.exists())
                    self.assertFalse((self.root/'escape').exists())

    def test_restore_rejects_unsafe_tar_member_before_writing(self):
        manifest = cp.save(self.work, self.package, ID, limit=97)
        shutil.rmtree(self.work)
        for name in ('../escape', '/escape', 'a/../escape', 'a//b'):
            with self.subTest(name=name):
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode='w') as archive:
                    member = tarfile.TarInfo(name)
                    member.size = 1
                    member.pax_headers['CEF.mtime_ns'] = '0'
                    archive.addfile(member, io.BytesIO(b'x'))
                part = self.package/'workspace.tar.gz.part0000'
                part.write_bytes(gzip.compress(data.getvalue()))
                bad = dict(manifest, files=1, unpacked_bytes=1, parts=[
                    {'name': part.name, 'bytes': part.stat().st_size, 'sha256': cp.digest(part)}])
                (self.package/'checkpoint.json').write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    cp.restore(self.package, self.work, ID)
                self.assertFalse(self.work.exists())
                self.assertFalse((self.root/'escape').exists())
                self.assertFalse(list(self.root.glob('cef-restore-*')))


if __name__ == '__main__':
    unittest.main()
