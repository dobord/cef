"""Real filesystem regressions for Chromium/depot_tools checkpoints, not CEF builds."""
from pathlib import Path
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'static'))
import checkpoint as cp

IDENTITY = {'recipe': 'symlink-regression-not-engine'}


class CheckpointLinksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.work = self.root/'workspace'
        (self.work/'depot_tools').mkdir(parents=True)
        self.file = self.work/'depot_tools/cros'
        self.file.write_bytes(b'fixture, not depot_tools executable\n')
        self.snapshot = self.root/'checkpoint'

    def tearDown(self):
        self.temporary.cleanup()

    def save_restore(self):
        result = cp.save(self.work, self.snapshot, IDENTITY, limit=127)
        shutil.rmtree(self.work)
        cp.restore(self.snapshot, self.work, IDENTITY)
        return result

    def test_cros_sdk_symlink_and_object_timestamp_roundtrip(self):
        os.symlink('cros', self.work/'depot_tools/cros_sdk')
        obj = self.work/'out/already-built.obj'; obj.parent.mkdir()
        obj.write_bytes(b'fixture-object')
        timestamp = 1712345678123456700
        os.utime(obj, ns=(timestamp, timestamp))
        audit = cp.preflight(self.work)
        self.assertEqual(audit['links'], 1)
        self.assertEqual(audit['files'], 2)
        result = self.save_restore()
        self.assertEqual(len(result['links']), 1)
        self.assertTrue((self.work/'depot_tools/cros_sdk').is_symlink())
        self.assertEqual(os.readlink(self.work/'depot_tools/cros_sdk'), 'cros')
        self.assertEqual((self.work/'depot_tools/cros_sdk').read_bytes(), self.file.read_bytes())
        self.assertEqual(obj.stat().st_mtime_ns, timestamp)
        self.assertFalse(result['engine_runtime_verified'])

    def test_internal_directory_link_does_not_duplicate_files(self):
        (self.work/'generated').mkdir()
        (self.work/'generated/value').write_bytes(b'one copy')
        os.symlink(str(Path('..')/'generated'), self.work/'depot_tools/generated', target_is_directory=True)
        result = self.save_restore()
        self.assertEqual(result['files'], 2)
        self.assertEqual(result['links'][0]['target'], '../generated')
        self.assertEqual(os.readlink(self.work/'depot_tools/generated'), str(Path('..')/'generated'))
        self.assertEqual((self.work/'depot_tools/generated/value').read_bytes(), b'one copy')

    def test_empty_directory_target_survives(self):
        (self.work/'empty').mkdir()
        os.symlink('empty', self.work/'empty-alias', target_is_directory=True)
        self.save_restore()
        self.assertTrue((self.work/'empty-alias').is_dir())

    def test_dangling_internal_link_survives(self):
        os.symlink('generated-later', self.work/'later')
        self.save_restore()
        self.assertTrue((self.work/'later').is_symlink())
        self.assertFalse((self.work/'later').exists())

    def test_external_relative_link_rejected_before_archive(self):
        os.symlink(str(Path('..')/'external'), self.work/'outside')
        with self.assertRaises(ValueError): cp.preflight(self.work)
        with self.assertRaises(ValueError): cp.save(self.work, self.snapshot, IDENTITY)
        self.assertFalse((self.snapshot/'checkpoint.json').exists())

    def test_absolute_link_rejected(self):
        os.symlink(str(self.file), self.work/'absolute')
        with self.assertRaises(ValueError): cp.preflight(self.work)

    def test_cycles_rejected(self):
        os.symlink('b', self.work/'a'); os.symlink('a', self.work/'b')
        with self.assertRaises(ValueError): cp.preflight(self.work)

    def test_relative_internal_link_chain_roundtrip(self):
        os.symlink(str(Path('depot_tools')/'cros'), self.work/'b')
        os.symlink('b', self.work/'a')
        self.save_restore()
        self.assertEqual((self.work/'a').read_bytes(), self.file.read_bytes())

    def test_perf_credentials_omitted_without_reading(self):
        secret = self.work/next(iter(cp.OMITTED_FILES))
        secret.parent.mkdir(parents=True); secret.write_bytes(b'SYNTHETIC-SECRET-MUST-NOT-ESCAPE')
        original_open = Path.open
        def no_secret_open(path, *args, **kwargs):
            self.assertNotEqual(path, secret, 'Credential bytes must not be read')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', no_secret_open):
            result = self.save_restore()
        self.assertEqual(result['omitted_files'], [secret.relative_to(self.work).as_posix()])
        self.assertFalse(secret.exists())
        with cp.PartsReader(self.snapshot, result['parts']) as raw, io.BufferedReader(raw) as stream:
            with tarfile.open(fileobj=stream, mode='r|gz') as archive:
                for member in archive:
                    self.assertNotEqual(member.name, result['omitted_files'][0])
                    self.assertNotIn(b'SYNTHETIC-SECRET-MUST-NOT-ESCAPE', archive.extractfile(member).read())

    def test_other_credentials_are_not_silently_allowed(self):
        (self.work/'credentials.json').write_bytes(b'SYNTHETIC')
        with self.assertRaisesRegex(ValueError, 'Credentials'): cp.preflight(self.work)
        with self.assertRaisesRegex(ValueError, 'Credentials'): cp.save(self.work, self.snapshot, IDENTITY)
        self.assertFalse((self.snapshot/'checkpoint.json').exists())

    def test_existing_git_credential_guard_preserved(self):
        d = self.work/'depot_tools/.git'; d.mkdir()
        (d/'config').write_text('[http]\nextraheader = SYNTHETIC\n')
        with self.assertRaisesRegex(ValueError, 'Credential'): cp.preflight(self.work)

    def test_malicious_link_manifest_rejected_without_escape(self):
        result = cp.save(self.work, self.snapshot, IDENTITY)
        shutil.rmtree(self.work)
        result['links'] = [{'name': 'outside', 'target': '../escaped', 'directory': True}]
        (self.snapshot/'checkpoint.json').write_text(json.dumps(result))
        with self.assertRaises(ValueError): cp.restore(self.snapshot, self.work, IDENTITY)
        self.assertFalse((self.root/'escaped').exists())
        self.assertFalse(self.work.exists())

    def test_directory_link_cannot_redirect_file_writes(self):
        result = cp.save(self.work, self.snapshot, IDENTITY)
        shutil.rmtree(self.work)
        result['links'] = [{'name': 'depot_tools', 'target': '.', 'directory': True}]
        (self.snapshot/'checkpoint.json').write_text(json.dumps(result))
        with self.assertRaises(ValueError): cp.restore(self.snapshot, self.work, IDENTITY)
        self.assertFalse(self.work.exists())

    def test_unknown_exclusion_is_rejected(self):
        result = cp.save(self.work, self.snapshot, IDENTITY)
        shutil.rmtree(self.work)
        result['omitted_files'] = ['out/important.obj']
        (self.snapshot/'checkpoint.json').write_text(json.dumps(result))
        with self.assertRaises(ValueError): cp.restore(self.snapshot, self.work, IDENTITY)

    def test_windows_and_posix_escape_targets_rejected(self):
        for target in ('/tmp/x', 'C:/x', 'C:x', r'\\server\share', '../../x', '\x00x'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                cp.link_target('depot_tools/cros_sdk', target)


if __name__ == '__main__':
    unittest.main()
