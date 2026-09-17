"""Regression for image rollout, strict legacy migration and native file hashes."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

STATIC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STATIC))
import checkpoint
import checkpoint_probe as probe
import resume_checkpoint as resume
import runner_fingerprint as fp
import runner_image_migration as migration


def observed():
    components = {'test-compiler': {'path': 'C:/reviewed/toolchain', 'files': 1,
                  'bytes': 10, 'sha256': 'a'*64}}
    return {'schema': 1, 'components': components,
            'sha256': hashlib.sha256(fp.json_bytes(components)).hexdigest()}


def transition():
    return {'from_image': '20260907.297.1', 'to_image': '20260913.307.1',
            'toolchain_sha256': observed()['sha256'],
            'collector_sha256': hashlib.sha256(Path(fp.__file__).read_text(
                encoding='utf-8').encode('utf-8')).hexdigest(),
            'evidence_run': 35177873930, 'evidence_sha': 'a'*40}


class InventoryTests(unittest.TestCase):
    def test_actual_files_content_and_names_not_clocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); first = root/'compiler'; first.write_bytes(b'ABC')
            stamp = first.stat().st_mtime_ns
            before = fp.inventory(root)
            os.utime(first, ns=(stamp+1000000, stamp+1000000))
            self.assertEqual(before, fp.inventory(root))
            first.write_bytes(b'ABD'); os.utime(first, ns=(stamp, stamp))
            self.assertNotEqual(before['sha256'], fp.inventory(root)['sha256'])
            first.write_bytes(b'ABC'); first.rename(root/'other-compiler')
            self.assertNotEqual(before['sha256'], fp.inventory(root)['sha256'])

    def test_missing_and_empty_components_fail(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError): fp.inventory(Path(td))
            with self.assertRaises(FileNotFoundError): fp.inventory(Path(td)/'missing')

    def test_inventory_is_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'compiler'; path.write_bytes(b'compiler bytes')
            before = (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            fp.inventory(path)
            self.assertEqual(before, (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns))

    def test_content_digest_required_not_a_string_claim(self):
        self.assertEqual(migration.checked_digest(observed()), observed()['sha256'])
        for value in [{}, {'schema': 1, 'sha256': observed()['sha256']},
                      dict(observed(), sha256='0'*64), dict(observed(), schema=True)]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                migration.checked_digest(value)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.receipt = Path(self.tmp.name)/'evidence.json'
        self.identity = {'platform':'windows-x64', 'image':'20260913.307.1',
                         'recipe':'b'*64, 'work':'D:/a/_temp/cef-static',
                         'repository':'owner/repo', 'ref':'refs/heads/static-engine', 'schema':3}

    def apply(self, identity=None, review=None, collector=None):
        return migration.input_identity(identity or self.identity,
            transition() if review is None else review, self.receipt,
            collect=collector or (lambda: observed()))

    def test_only_input_image_changes_and_actual_environment_is_preserved(self):
        before = dict(self.identity)
        with mock.patch.dict(os.environ, {'ImageVersion':before['image']}):
            result = self.apply()
            self.assertEqual(os.environ['ImageVersion'], before['image'])
        self.assertEqual(self.identity, before)
        self.assertEqual(result, dict(before, image='20260907.297.1'))
        evidence = json.loads(self.receipt.read_text())
        self.assertEqual(evidence['status'], 'verified')
        self.assertTrue(evidence['image_transition_used'])
        self.assertFalse(evidence['engine_runtime_verified'])

    def test_same_reviewed_image_still_checks_bytes(self):
        old = dict(self.identity, image='20260907.297.1')
        collect = mock.Mock(return_value=observed())
        self.assertEqual(self.apply(old, collector=collect), old)
        collect.assert_called_once_with()
        self.assertFalse(json.loads(self.receipt.read_text())['image_transition_used'])

    def test_wrong_image_platform_collector_and_missing_evidence_fail_closed(self):
        cases = [(dict(self.identity, image='20261001.1.0'), transition()),
                 (dict(self.identity, platform='linux-x64'), transition()),
                 (self.identity, dict(transition(), collector_sha256='0'*64)),
                 (self.identity, dict(transition(), evidence_run=0)),
                 (self.identity, {})]
        for identity, review in cases:
            collect = mock.Mock(return_value=observed())
            with self.subTest(identity=identity, review=review), self.assertRaises(ValueError):
                self.apply(identity, review, collect)
            collect.assert_not_called()
            self.assertEqual(json.loads(self.receipt.read_text())['status'], 'failed')

    def test_changed_build_inputs_or_collection_failure_are_not_ignored(self):
        for collector in [mock.Mock(return_value=dict(observed(), sha256='0'*64)),
                          mock.Mock(side_effect=PermissionError('unreadable compiler'))]:
            with self.assertRaises((ValueError, PermissionError)):
                self.apply(collector=collector)
            self.assertEqual(json.loads(self.receipt.read_text())['status'], 'failed')
        with self.assertRaises(ValueError):
            self.apply(review=dict(transition(), toolchain_sha256='0'*64))

    def test_real_archive_rejects_old_image_then_accepts_only_reviewed_identity(self):
        base = Path(self.tmp.name)
        producer = base/'producer'; producer.mkdir(); (producer/'object').write_bytes(b'object')
        package = base/'checkpoint'; old = dict(self.identity, image='20260907.297.1')
        checkpoint.save(producer, package, old, limit=64)
        target = base/'consumer'
        with self.assertRaisesRegex(ValueError, 'identity/schema mismatch'):
            checkpoint.restore(package, target, self.identity)
        self.assertFalse(target.exists())
        checkpoint.restore(package, target, self.apply())
        self.assertEqual((target/'object').read_bytes(), b'object')
        for field in ['recipe', 'work', 'repository', 'ref', 'schema']:
            wrong = dict(self.identity); wrong[field] = 'untrusted'
            with self.subTest(field=field), self.assertRaises(ValueError):
                checkpoint.restore(package, base/('bad-'+field), self.apply(wrong))

    def test_producer_binding_before_any_image_migration(self):
        root = Path(self.tmp.name); ledger = root/'vcpkg/static/checkpoint-migrations.json'
        ledger.parent.mkdir(parents=True)
        rule = {'platform':'windows-x64', 'from_recipe':'a'*64, 'to_recipe':'b'*64,
                'producer_run':123, 'producer_sha':'c'*40, 'producer_attempt':1,
                'image_transition':transition()}
        ledger.write_text(json.dumps({'schema':1,'migrations':[rule]}))
        run = {'id':123, 'head_sha':'c'*40, 'run_attempt':1}
        with mock.patch.object(resume, 'ROOT', root), mock.patch.object(
                migration, 'input_identity', return_value=self.identity) as apply:
            for field, bad in [('id',124), ('head_sha','d'*40), ('run_attempt',2)]:
                self.assertEqual(resume.checkpoint_input_identity(self.identity, dict(run, **{field:bad})),
                                 (self.identity, None))
            apply.assert_not_called()
            resume.checkpoint_input_identity(self.identity, run)
            apply.assert_called_once()
            self.assertEqual(apply.call_args.args[0]['recipe'], 'a'*64)

    def test_no_transition_keeps_existing_image_contract(self):
        root = Path(self.tmp.name); ledger = root/'vcpkg/static/checkpoint-migrations.json'
        ledger.parent.mkdir(parents=True)
        rule = {'platform':'windows-x64', 'from_recipe':'a'*64, 'to_recipe':'b'*64,
                'producer_run':123, 'producer_sha':'c'*40, 'producer_attempt':1}
        ledger.write_text(json.dumps({'schema':1,'migrations':[rule]}))
        with mock.patch.object(resume, 'ROOT', root):
            result, _ = resume.checkpoint_input_identity(self.identity,
                {'id':123,'head_sha':'c'*40,'run_attempt':1})
        self.assertEqual(result, dict(self.identity, recipe='a'*64))


class FixtureIdentityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Native Windows fixture identity')
    def test_windows_rollout_requires_equal_content_not_equal_image_labels(self):
        work = Path(tempfile.gettempdir())/'fixture'
        with mock.patch.dict(os.environ, {'ImageVersion':'20260907.297.1'}):
            before = probe.fixture_identity(work, observed())
        with mock.patch.dict(os.environ, {'ImageVersion':'20260913.307.1'}):
            after = probe.fixture_identity(work, observed())
        self.assertEqual(before, after)
        self.assertNotIn('image', before)
        changed = observed(); changed['components']['test-compiler']['sha256'] = 'b'*64
        changed['sha256'] = hashlib.sha256(fp.json_bytes(changed['components'])).hexdigest()
        self.assertNotEqual(before, probe.fixture_identity(work, changed))

    @unittest.skipIf(os.name == 'nt', 'Linux image policy')
    def test_linux_policy_is_not_relaxed(self):
        with mock.patch.dict(os.environ, {'ImageVersion':'one'}):
            before = probe.fixture_identity(Path(tempfile.gettempdir()))
        with mock.patch.dict(os.environ, {'ImageVersion':'two'}):
            after = probe.fixture_identity(Path(tempfile.gettempdir()))
        self.assertNotEqual(before, after)


if __name__ == '__main__':
    unittest.main()
