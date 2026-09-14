"""Cross-run checkpoint selection. Passing tests are not a completed CEF build."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import resume_checkpoint as resume

REPO = 'dobord/cef'
BRANCH = 'static-engine'
IDENTITY = {'repository': REPO, 'ref': 'refs/heads/'+BRANCH,
            'platform': 'linux-x64', 'recipe': 'test', 'schema': 3}
PREFIX = 'cef-linux-checkpoint-identity'


def producer(run_id=50, attempt=2, **updates):
    result = {'id': run_id, 'run_attempt': attempt, 'status': 'completed', 'conclusion': 'success',
              'head_sha': 'a'*40, 'head_branch': BRANCH, 'repository': {'full_name': REPO},
              'head_repository': {'full_name': REPO}, 'path': resume.WORKFLOW, 'event': 'push',
              'created_at': f'2026-09-13T08:00:{run_id % 60:02d}Z'}
    result.update(updates)
    return result


def artifact(run=None, **updates):
    run = run or producer()
    result = {'id': 999, 'name': f"{PREFIX}-{run['id']}-{run['run_attempt']}", 'expired': False,
              'size_in_bytes': 123, 'digest': 'sha256:'+'b'*64,
              'workflow_run': {'id': run['id'], 'head_sha': run['head_sha'], 'head_branch': run['head_branch']}}
    result.update(updates)
    return result


class SelectionTests(unittest.TestCase):
    def test_choose_latest_completed_producer_not_current_or_foreign(self):
        current = producer(60)
        source = producer(50)
        foreign = producer(70, head_repository={'full_name': 'other/cef'})
        api = Mock(side_effect=[{'workflow_runs': [producer(40), current, source, foreign]}, source])
        self.assertEqual(resume.choose_run(IDENTITY, 60, '', api), source)
        self.assertIn('/actions/runs?branch=static-engine&', api.call_args_list[0].args[0])

    def test_latest_active_run_does_not_rewind_to_older_completed(self):
        active = producer(50, status='in_progress')
        api = Mock(side_effect=[{'workflow_runs': [producer(40), active]}, active])
        with self.assertRaisesRegex(ValueError, 'still active'):
            resume.choose_run(IDENTITY, 60, '', api)
        self.assertEqual(api.call_count, 2)

    def test_explicit_producer_is_loaded_without_discovery(self):
        api = Mock(return_value=producer())
        self.assertEqual(resume.choose_run(IDENTITY, 60, '50', api)['id'], 50)
        api.assert_called_once_with('repos/dobord/cef/actions/runs/50')

    def test_invalid_producer_id_rejected_before_api(self):
        for value in ('../50', '-1', '0', '1?token=x', '50/attempts/1', 'true'):
            api = Mock()
            with self.subTest(value=value), self.assertRaises(ValueError):
                resume.choose_run(IDENTITY, 60, value, api)
            api.assert_not_called()

    def test_current_run_rejected_before_download(self):
        api = Mock()
        with self.assertRaisesRegex(ValueError, 'own workflow'):
            resume.choose_run(IDENTITY, 50, '50', api)
        api.assert_not_called()

    def test_absent_producer_requires_explicit_fresh(self):
        api = Mock(return_value={'workflow_runs': []})
        with self.assertRaisesRegex(ValueError, 'checkpoint_mode=fresh'):
            resume.choose_run(IDENTITY, 60, '', api)

    def test_untrusted_explicit_producer_rejected(self):
        for field, value in [('head_repository', {'full_name': 'attacker/cef'}),
                             ('repository', {'full_name': 'other/cef'}),
                             ('event', 'pull_request'), ('head_branch', 'other'),
                             ('path', '.github/workflows/untrusted.yml')]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'untrusted'):
                resume.choose_run(IDENTITY, 60, '50', Mock(return_value=producer(**{field: value})))

    def test_invalid_repo_or_tag_not_interpolated_into_api(self):
        for identity in [dict(IDENTITY, repository='../invalid'), dict(IDENTITY, ref='refs/tags/release')]:
            api = Mock()
            with self.assertRaises(ValueError): resume.choose_run(identity, 60, '', api)
            api.assert_not_called()

    def test_branch_query_is_encoded(self):
        run = producer(head_branch='feature/checkpoint')
        identity = dict(IDENTITY, ref='refs/heads/feature/checkpoint')
        api = Mock(side_effect=[{'workflow_runs': [run]}, run])
        resume.choose_run(identity, 60, '', api)
        self.assertIn('branch=feature%2Fcheckpoint', api.call_args_list[0].args[0])

    def test_run_discovery_paginates(self):
        api = Mock(side_effect=[{'workflow_runs': [producer(10, event='pull_request')]*100},
                                {'workflow_runs': [producer()]}, producer()])
        resume.choose_run(IDENTITY, 60, '', api)
        self.assertIn('page=2', api.call_args_list[1].args[0])

    def test_select_latest_attempt_despite_larger_old_artifact_id(self):
        old = artifact(producer(attempt=1), id=99999)
        current = artifact()
        api = Mock(return_value={'artifacts': [old, current]})
        self.assertEqual(resume.choose_artifact(IDENTITY, producer(), PREFIX, api), current)

    def test_missing_latest_attempt_does_not_accept_old_attempt(self):
        api = Mock(return_value={'artifacts': [artifact(producer(attempt=1))]})
        with self.assertRaisesRegex(ValueError, 'No older checkpoint or fresh build'):
            resume.choose_artifact(IDENTITY, producer(), PREFIX, api)
        api.assert_called_once()

    def test_missing_checkpoint_does_not_search_an_older_run(self):
        api = Mock(return_value={'artifacts': []})
        with self.assertRaisesRegex(ValueError, 'missing'):
            resume.choose_artifact(IDENTITY, producer(), PREFIX, api)
        api.assert_called_once_with('repos/dobord/cef/actions/runs/50/artifacts?per_page=100&page=1')

    def test_wrong_image_or_recipe_prefix_not_accepted(self):
        with self.assertRaisesRegex(ValueError, 'missing'):
            resume.choose_artifact(IDENTITY, producer(), 'different-identity',
                                   Mock(return_value={'artifacts': [artifact()]}))

    def test_expired_and_wrong_origin_or_size_rejected(self):
        for updates in [{'expired': True}, {'size_in_bytes': 0}, {'id': 0},
                        {'workflow_run': {'id': 49}},
                        {'workflow_run': {'id': 50, 'head_sha': 'different', 'head_branch': BRANCH}}]:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                resume.choose_artifact(IDENTITY, producer(), PREFIX,
                                       Mock(return_value={'artifacts': [artifact(**updates)]}))

    def test_duplicate_candidate_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            resume.choose_artifact(IDENTITY, producer(), PREFIX,
                                   Mock(return_value={'artifacts': [artifact(), artifact()]}))

    def test_artifact_list_paginates(self):
        api = Mock(side_effect=[{'artifacts': [{'name': 'diagnostics'}]*100},
                                {'artifacts': [artifact()]}])
        self.assertEqual(resume.choose_artifact(IDENTITY, producer(), PREFIX, api)['id'], 999)
        self.assertIn('page=2', api.call_args.args[0])

    def test_api_failure_is_not_cache_miss(self):
        with self.assertRaisesRegex(RuntimeError, 'network failure'):
            resume.choose_run(IDENTITY, 60, '', Mock(side_effect=RuntimeError('network failure')))

    def test_only_first_attempt_runs_compilation(self):
        resume.new_run_only('1')
        for attempt in ('2', '10', '0', '', True):
            with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                resume.new_run_only(attempt)


class RequestTests(unittest.TestCase):
    def test_committed_marker_is_explicit_and_not_a_recipe_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path = root/'vcpkg/static/iteration-request.json'; path.parent.mkdir(parents=True)
            for mode in ('resume', 'fresh'):
                path.write_text(json.dumps({'schema': 1, 'sequence': 2, 'mode': mode, 'checkpoint_run': '50'}))
                with patch.object(resume, 'ROOT', root), patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'push'}):
                    self.assertEqual(resume.push_request('resume', ''), (mode, '50', mode == 'fresh'))

    def test_dispatch_options_override_marker(self):
        with patch.object(resume, 'ROOT', Path('/nonexistent')), \
             patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch'}):
            self.assertEqual(resume.push_request('resume', '70'), ('resume', '70', False))

    def test_bad_marker_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path = root/'vcpkg/static/iteration-request.json'; path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'schema': 1, 'sequence': 0, 'mode': 'resume', 'checkpoint_run': ''}))
            with patch.object(resume, 'ROOT', root), patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'push'}), \
                 self.assertRaisesRegex(ValueError, 'Invalid committed'):
                resume.push_request('resume', '')

    def test_workflow_uses_new_selector_for_both_platforms(self):
        text = (resume.ROOT/'.github/workflows/static-engine-build.yml').read_text()
        self.assertEqual(text.count('run: python vcpkg/static/resume_checkpoint.py restore'), 2)
        self.assertNotIn('run: python vcpkg/static/checkpoint.py restore', text)
        self.assertNotIn('run: python vcpkg/static/linux_checkpoint.py restore', text)
        self.assertLess(text.index('resume_checkpoint.py guard'), text.index('Test native launchers'))
        self.assertLess(text.index('resume_checkpoint.py audit-before'), text.index('      - name: Bounded Windows'))
        self.assertLess(text.index('Persist Ubuntu progress'), text.index('resume_checkpoint.py audit-after'))
        self.assertLess(text.index('resume_checkpoint.py audit-after'), text.index('      - name: Build through vcpkg'))
        self.assertNotIn('actions: write', text)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.work = self.root/'work'; self.package = self.root/'download'
        self.adapter = Mock()
        self.adapter.restore.return_value = {'files': 10, 'unpacked_bytes': 100, 'parts': [1, 2]}

    def download(self, *args, **kwargs):
        self.package.mkdir()

    def test_precise_run_attempt_artifact_receipt(self):
        api = Mock(return_value=producer())
        result = resume.restore_selected(self.adapter, self.work, self.package, IDENTITY,
                                         producer(), artifact(), api=api, download=self.download)
        self.assertEqual((result['restored_from_run'], result['restored_from_attempt'], result['artifact_id']), (50, 2, 999))
        self.assertFalse(result['engine_runtime_verified'])
        self.adapter.restore.assert_called_once_with(self.package, self.work, IDENTITY)
        self.assertFalse(self.package.exists())
        self.assertEqual(api.call_count, 2)

    def test_producer_retry_during_download_never_extracts(self):
        api = Mock(side_effect=[producer(), producer(attempt=3, status='in_progress')])
        with self.assertRaisesRegex(ValueError, 're-run or changed'):
            resume.restore_selected(self.adapter, self.work, self.package, IDENTITY,
                                    producer(), artifact(), api=api, download=self.download)
        self.adapter.restore.assert_not_called()

    def test_existing_source_is_never_erased(self):
        self.work.mkdir(); (self.work/'important').write_text('unchanged')
        download = Mock()
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            resume.restore_selected(self.adapter, self.work, self.package, IDENTITY,
                                    producer(), artifact(), api=Mock(), download=download)
        download.assert_not_called()
        self.assertEqual((self.work/'important').read_text(), 'unchanged')

    def test_existing_download_never_merged(self):
        self.package.mkdir()
        with self.assertRaisesRegex(ValueError, 'merge'):
            resume.restore_selected(self.adapter, self.work, self.package, IDENTITY,
                                    producer(), artifact(), api=Mock(), download=Mock())

    def test_corruption_propagates_and_no_restore_receipt(self):
        self.adapter.restore.side_effect = ValueError('Checkpoint checksum mismatch')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            resume.restore_selected(self.adapter, self.work, self.package, IDENTITY,
                                    producer(), artifact(), api=Mock(return_value=producer()), download=self.download)
        self.assertFalse(self.work.exists())

    def test_unrelated_repository_metadata_change_is_not_a_new_attempt(self):
        current = producer(repository={'full_name': REPO, 'description': 'updated'})
        resume.verify_stable(producer(), REPO, Mock(return_value=current))

    @unittest.skipUnless(sys.platform == 'linux', 'production POSIX adapter requires Linux')
    def test_real_archive_reused_through_new_controller(self):
        import linux_checkpoint as cp
        self.work.mkdir(); (self.work/'test.o').write_bytes(b'archive fixture, not compiled CEF')
        original = self.root/'original'
        cp.save(self.work, original, IDENTITY, limit=71)
        shutil.rmtree(self.work)
        def download(*args, **kwargs): shutil.copytree(original, self.package)
        result = resume.restore_selected(cp, self.work, self.package, IDENTITY,
                                         producer(), artifact(), api=Mock(return_value=producer()), download=download)
        self.assertEqual(result['files'], 1)
        self.assertEqual((self.work/'test.o').read_bytes(), b'archive fixture, not compiled CEF')


class ProgressTests(unittest.TestCase):
    def test_repeating_old_edges_is_stalled(self):
        self.assertEqual(resume.progress_report({'a.obj'}, {'a.obj'}, False)['status'], 'stalled')

    def test_new_output_records_progress_not_runtime_success(self):
        result = resume.progress_report({'a.obj'}, {'a.obj', 'b.obj'}, False)
        self.assertEqual((result['status'], result['new_compiled_outputs']), ('progress', 1))
        self.assertFalse(result['engine_runtime_verified'])

    def test_completed_engine_allows_validation_without_new_output(self):
        result = resume.progress_report({'a.obj'}, {'a.obj'}, True)
        self.assertEqual(result['status'], 'progress')
        self.assertFalse(result['engine_runtime_verified'])

    def test_log_compaction_does_not_manufacture_progress(self):
        self.assertEqual(resume.progress_report({'a.obj', 'b.obj'}, {'a.obj'}, False)['status'], 'stalled')

    def test_ninja_log_reader_and_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(resume.ninja_outputs(root), set())
            log = root/'download/chromium/src/out/CEF_Static_Release_x64/.ninja_log'
            log.parent.mkdir(parents=True)
            log.write_text('# ninja log v5\n0\t2\t123\tobj/a.obj\taaaa\n0\t4\t124\tobj/a.obj\tbbbb\n')
            self.assertEqual(resume.ninja_outputs(root), {'obj/a.obj'})
            log.write_text('# ninja log v5\nbroken\n')
            with self.assertRaisesRegex(ValueError, 'Malformed'): resume.ninja_outputs(root)

    def test_audit_after_writes_stalled_receipt_before_failing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); diag = root/'static-diagnostics/resume'; diag.mkdir(parents=True)
            (diag/'ninja-before.json').write_text(json.dumps({'outputs': ['a.obj']}))
            with patch.object(resume, 'ROOT', root), patch.object(resume, 'work_path', return_value=root), \
                 patch.object(resume, 'ninja_outputs', return_value={'a.obj'}), \
                 patch.dict(os.environ, {'CEF_ITERATION_READY':'false', 'GITHUB_STEP_SUMMARY':str(root/'summary')}), \
                 self.assertRaisesRegex(ValueError, 'No additional Ninja outputs'):
                resume.audit_main('audit-after')
            self.assertEqual(json.loads((diag/'progress.json').read_text())['status'], 'stalled')

    @unittest.skipUnless(sys.platform == 'linux' and shutil.which('ninja') and shutil.which('cc'),
                         'requires Linux native C compiler and Ninja')
    def test_real_ninja_noop_then_an_additional_object(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            build = root/'download/chromium/src/out/CEF_Static_Release_x64'; build.mkdir(parents=True)
            (build/'a.c').write_text('int a(void) { return 1; }\n')
            (build/'b.c').write_text('int b(void) { return 2; }\n')
            (build/'build.ninja').write_text('rule cc\n  command = cc -c $in -o $out\n'
                                           'build a.o: cc a.c\nbuild b.o: cc b.c\n')
            def ninja(target): subprocess.run(['ninja','-C',str(build),target],check=True,capture_output=True)
            ninja('a.o'); before = resume.ninja_outputs(root)
            ninja('a.o'); self.assertEqual(resume.progress_report(before,resume.ninja_outputs(root),False)['status'],'stalled')
            ninja('b.o'); self.assertEqual(resume.progress_report(before,resume.ninja_outputs(root),False)['new_compiled_outputs'],1)


if __name__ == '__main__':
    unittest.main()
