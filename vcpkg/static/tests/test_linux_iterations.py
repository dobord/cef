"""Ubuntu checkpoints and gating. A passing fixture is not a CEF build."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

STATIC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STATIC))
import linux_checkpoint as cp
import linux_slice as sliced

ID = {'platform': 'linux-x64', 'recipe': 'linux-test', 'schema': cp.SCHEMA}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root/'work'
        self.work.mkdir()
        self.package = self.root/'snapshot'
        (self.work/'object.o').write_bytes(b'fixture, not a CEF object')

    def save(self):
        return cp.save(self.work, self.package, ID, limit=83)

    def restore(self):
        shutil.rmtree(self.work)
        return cp.restore(self.package, self.work, ID)

    def test_case_sensitive_directories_files_and_links(self):
        for name, value in [('Case', 'A'), ('case', 'B')]:
            (self.work/name).mkdir()
            (self.work/name/'file').write_text(value)
            os.symlink(name+'/file', self.work/(name+'.link'))
        self.save(); self.restore()
        self.assertEqual((self.work/'Case.link').read_text(), 'A')
        self.assertEqual((self.work/'case.link').read_text(), 'B')

    def test_hardlinks_are_restored_as_complete_regular_files(self):
        os.link(self.work/'object.o', self.work/'hardlink.o')
        data = (self.work/'object.o').read_bytes()
        self.save(); self.restore()
        self.assertEqual((self.work/'hardlink.o').read_bytes(), data)
        self.assertEqual((self.work/'object.o').read_bytes(), data)

    def test_modes_and_nanosecond_clocks(self):
        p = self.work/'tool'
        p.write_text('#!/bin/sh\necho OK\n'); p.chmod(0o755)
        os.utime(p, ns=(1700000000123456789, 1700000000123456789))
        os.symlink('tool', self.work/'alias')
        cp.set_link_mtime(self.work/'alias', 1700000000999999999)
        self.save(); self.restore()
        self.assertEqual(p.stat().st_mtime_ns, 1700000000123456789)
        self.assertEqual((self.work/'alias').lstat().st_mtime_ns, 1700000000999999999)
        self.assertEqual(p.stat().st_mode & 0o777, 0o755)
        self.assertEqual(subprocess.check_output([p], text=True).strip(), 'OK')

    def test_corruption_fails_before_extraction(self):
        m = self.save()
        p = self.package/m['parts'][0]['name']; p.write_bytes(b'bad')
        shutil.rmtree(self.work)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            cp.restore(self.package, self.work, ID)
        self.assertFalse(self.work.exists())

    def test_cross_platform_identity_is_not_accepted(self):
        self.save(); shutil.rmtree(self.work)
        with self.assertRaisesRegex(ValueError, 'identity'):
            cp.restore(self.package, self.work, dict(ID, platform='windows-x64'))

    def test_never_overwrite_existing_workspace(self):
        self.save()
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            cp.restore(self.package, self.work, ID)
        self.assertTrue((self.work/'object.o').exists())

    def test_external_symlink_rejected(self):
        os.symlink('/etc/passwd', self.work/'escape')
        with self.assertRaises(ValueError): self.save()
        self.assertFalse((self.package/'checkpoint.json').exists())

    def test_credential_and_git_token_guards(self):
        (self.work/'credentials.json').write_text('synthetic-not-a-real-secret')
        with self.assertRaises(ValueError): self.save()
        self.assertFalse((self.package/'checkpoint.json').exists())

    def test_telemetry_omitted_not_allowed(self):
        secret = self.work/next(iter(cp.OMITTED_FILES)); secret.parent.mkdir(parents=True)
        secret.write_text('synthetic-not-a-real-secret')
        self.save(); self.restore()
        self.assertFalse(secret.exists())

    def test_malicious_directory_traversal_rejected(self):
        m = self.save(); shutil.rmtree(self.work)
        m['directories'].append('../outside')
        (self.package/'checkpoint.json').write_text(json.dumps(m))
        with self.assertRaises(ValueError): cp.restore(self.package, self.work, ID)
        self.assertFalse((self.root/'outside').exists())

    def test_malicious_link_traversal_rejected(self):
        m = self.save(); shutil.rmtree(self.work)
        m['links'].append({'name': 'escape', 'target': '../outside',
                           'directory': False, 'mtime_ns': 0})
        (self.package/'checkpoint.json').write_text(json.dumps(m))
        with self.assertRaises(ValueError): cp.restore(self.package, self.work, ID)
        self.assertFalse((self.root/'outside').exists())

    def test_runtime_success_manifest_is_rejected(self):
        m = self.save(); shutil.rmtree(self.work); m['engine_runtime_verified'] = True
        (self.package/'checkpoint.json').write_text(json.dumps(m))
        with self.assertRaises(ValueError): cp.restore(self.package, self.work, ID)


class IdentityTests(unittest.TestCase):
    def test_platform_namespace_is_separate(self):
        self.assertTrue(cp.artifact_name(ID).startswith('cef-linux-checkpoint-'))
        self.assertNotEqual(cp.artifact_name(ID), cp.shared.artifact_name(ID))
        with self.assertRaises(ValueError): cp.artifact_name(dict(ID, platform='windows-x64'))

    def test_wrong_host_and_missing_image_are_rejected(self):
        with patch.object(cp.sys, 'platform', 'win32'), self.assertRaises(ValueError):
            cp.ci_identity(Path('/tmp/cef'))
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'ImageVersion'):
            cp.ci_identity(Path('/tmp/cef'))

    def test_operational_budget_does_not_invalidate_objects(self):
        with patch.dict(os.environ, {'ImageVersion': 'image', 'GITHUB_REPOSITORY': 'dobord/cef',
                                    'GITHUB_REF': 'refs/heads/static-engine'}):
            before = cp.ci_identity(Path('/tmp/work'))
            with patch.dict(os.environ, {'CEF_LINUX_SLICE_SECONDS': '100', 'GITHUB_RUN_ATTEMPT': '2'}):
                self.assertEqual(before, cp.ci_identity(Path('/tmp/work')))
            with patch.dict(os.environ, {'ImageVersion': 'changed'}):
                self.assertNotEqual(before, cp.ci_identity(Path('/tmp/work')))
            self.assertNotEqual(before, cp.ci_identity(Path('/tmp/other')))


class DiscoveryTests(unittest.TestCase):
    def test_previous_attempt_only_and_trusted_workflow(self):
        identity = dict(ID, repository='dobord/cef', ref='refs/heads/static-engine')
        origin = {'id': 101, 'head_branch': 'static-engine', 'head_sha': 'pinned'}
        artifact = {'id': 3, 'name': cp.artifact_name(identity)+'-101-1',
                    'expired': False, 'workflow_run': origin}
        run = {'status': 'in_progress', 'head_repository': {'full_name': 'dobord/cef'},
               'head_branch': 'static-engine', 'head_sha': 'pinned', 'event': 'push',
               'path': '.github/workflows/static-engine-build.yml'}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '101', 'GITHUB_RUN_ATTEMPT': '2'}), \
                 patch.object(cp.shared, 'gh_json', side_effect=[{'artifacts':[artifact]}, run]), \
                 patch.object(cp.subprocess, 'run') as download, patch.object(cp, 'restore', return_value={}), \
                 patch.object(cp.shutil, 'rmtree'):
                result = cp.restore_previous(root/'work', root/'download', identity)
                self.assertEqual(result['restored_from_attempt'], 1)
                download.assert_called_once()
            for field, value in [('event', 'pull_request'), ('head_sha', 'other'),
                                 ('path', 'untrusted.yml'), ('head_branch', 'other')]:
                with self.subTest(field=field), \
                     patch.dict(os.environ, {'GITHUB_RUN_ID': '101', 'GITHUB_RUN_ATTEMPT': '2'}), \
                     patch.object(cp.shared, 'gh_json', side_effect=[{'artifacts':[artifact]}, dict(run, **{field:value})]), \
                     patch.object(cp.subprocess, 'run') as download:
                    self.assertIsNone(cp.restore_previous(root/'work', root/'download', identity))
                    download.assert_not_called()
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '101', 'GITHUB_RUN_ATTEMPT': '1'}), \
                 patch.object(cp.shared, 'gh_json', return_value={'artifacts':[artifact]}), \
                 patch.object(cp.subprocess, 'run') as download:
                self.assertIsNone(cp.restore_previous(root/'work', root/'download', identity))
                download.assert_not_called()


class IterationTests(unittest.TestCase):
    def test_checkpoint_does_not_claim_runtime_success(self):
        self.exercise('checkpoint', 'ready=false')

    def test_completion_only_enables_later_validation(self):
        self.exercise('complete', 'ready=true')

    def test_failed_archive_cannot_enable_publication(self):
        self.exercise('checkpoint', '', fail_save=True)

    def exercise(self, status, ready, fail_save=False):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); output = root/'output'; summary = root/'summary'
            env = {'RUNNER_TEMP': td, 'CEF_STATIC_WORK': '', 'GITHUB_OUTPUT': str(output),
                   'GITHUB_STEP_SUMMARY': str(summary), 'CEF_LINUX_SLICE_SECONDS': '1', 'CEF_STATIC_JOBS': '1'}
            with patch.dict(os.environ, env), patch.object(sliced, 'ROOT', root), \
                 patch.object(sliced.checkpoint, 'ci_identity', return_value=ID), \
                 patch.object(sliced.build, 'setup_environment'), \
                 patch.object(sliced.build, 'prepare', return_value=root/'src'), \
                 patch.object(sliced.build, 'configuration', return_value=root/'out'), \
                 patch.object(sliced.build, 'find_binary', return_value=root/'ninja'), \
                 patch.object(sliced.checkpoint.shared, 'preflight', return_value={}), \
                 patch.object(sliced, 'run_ninja', return_value={'status':status,'engine_runtime_verified':False}), \
                 patch.object(sliced.checkpoint, 'save', side_effect=ValueError('archive rejected') if fail_save else None,
                              return_value={'files':1,'links':[],'omitted_files':[],'parts':[{'bytes':5}]}):
                if fail_save:
                    with self.assertRaises(ValueError): sliced.main()
                    self.assertFalse(output.exists())
                else:
                    sliced.main()
                    self.assertIn(ready, output.read_text())
                    self.assertIn('checkpoint_ready=true', output.read_text())
                    result=json.loads((root/'static-diagnostics/linux-iteration/iteration.json').read_text())
                    self.assertIs(result['engine_runtime_verified'], False)

    def test_invalid_budget_stops_before_prepare(self):
        for value in ('0', '10801', 'bad'):
            with patch.dict(os.environ, {'CEF_LINUX_SLICE_SECONDS':value}), \
                 patch.object(sliced.build, 'prepare') as prepare, self.assertRaises(ValueError):
                sliced.main()
            prepare.assert_not_called()

    def test_native_interruption_reuses_completed_edge(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root/'build.py').write_text('import pathlib,time\npathlib.Path("partial").write_text("incomplete")\ntime.sleep(20)\n')
            (root/'build.ninja').write_text('rule slow\n  command = "'+sys.executable+'" build.py\nbuild partial: slow\n')
            status=sliced.run_ninja([shutil.which('ninja')],root,root/'slice.log',1,grace=5)
            self.assertEqual(status['status'],'checkpoint'); self.assertFalse((root/'partial').exists())
            (root/'build.py').write_text('import pathlib\npathlib.Path("partial").write_text("complete")\n')
            status=sliced.run_ninja([shutil.which('ninja')],root,root/'resume.log',5)
            self.assertEqual(status['status'],'complete')


if __name__ == '__main__': unittest.main()
