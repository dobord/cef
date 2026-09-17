"""Publication policy tests; fixtures are never uploaded as real CEF SDKs."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import publish_sdk as pub
sdk = pub.sdk


def request():
    return {'schema': 1, 'sequence': 1, 'producer_run': 51, 'producer_attempt': 1,
            'producer_commit': 'a'*40, 'producer_branch': 'static-engine',
            'tag': 'cef-152.0.6-static-capi-r51-a1', 'prerelease': True}


def run():
    return {'id': 51, 'run_attempt': 1, 'head_sha': 'a'*40, 'head_branch': 'static-engine',
            'status': 'completed', 'conclusion': 'success', 'path': pub.WORKFLOW, 'event': 'push',
            'repository': {'full_name': pub.REPOSITORY}, 'head_repository': {'full_name': pub.REPOSITORY}}


def jobs():
    rows = [{'name': f'source-build (["host"], {t})', 'steps': [
                {'name': s, 'conclusion': 'success'} for s in (pub.RUNTIME_STEP, pub.UPLOAD_STEP)]}
            for t in sdk.TRIPLETS.values()]
    rows += [{'name': 'sdk-readiness'}, {'name': 'publish', 'conclusion': 'skipped'}]
    return [dict({'run_id': 51, 'run_attempt': 1, 'head_sha': 'a'*40,
                  'status': 'completed', 'conclusion': 'success'}, **j) for j in rows]


def artifacts():
    return [{'id': n, 'name': f'cef-static-{t}-sdk-1', 'expired': False,
             'size_in_bytes': 100, 'digest': 'sha256:'+'b'*64,
             'workflow_run': {'id': 51, 'head_sha': 'a'*40, 'head_branch': 'static-engine'}}
            for n, t in enumerate(sdk.TRIPLETS.values(), 1)]


class PolicyTests(unittest.TestCase):
    def test_request_is_explicit_and_prerelease_only(self):
        self.assertEqual(pub.request_data(request()), request())
        for key, value in [('producer_run', True), ('sequence', 0), ('producer_attempt', -1),
                           ('producer_commit', 'main'), ('producer_branch', 'feature/evil'),
                           ('prerelease', False), ('tag', 'v152.0.6'), ('schema', True)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                pub.request_data(dict(request(), **{key: value}))
        with self.assertRaises(ValueError):
            pub.request_data(dict(request(), token='not-a-real-token'))

    def test_unfinished_failed_foreign_and_rerun_producers_rejected(self):
        pub.validate_run(run(), request())
        for key, value in [('status', 'in_progress'), ('conclusion', 'failure'),
                           ('run_attempt', 2), ('head_sha', 'c'*40), ('id', 52),
                           ('head_branch', 'other'), ('event', 'pull_request'),
                           ('event', 'pull_request_target'), ('event', 'workflow_run'),
                           ('path', '.github/workflows/other.yml'),
                           ('repository', {'full_name': 'somewhere/else'}),
                           ('head_repository', {'full_name': 'somewhere/else'})]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                pub.validate_run(dict(run(), **{key: value}), request())

    def test_job_outcomes_require_both_real_consumers_and_uploads(self):
        pub.validate_jobs(jobs(), request())
        for bad in [jobs()[1:], jobs()+[jobs()[0]], [],
                    [dict(j, run_attempt=2) for j in jobs()],
                    [dict(j, head_sha='c'*40) for j in jobs()]]:
            with self.assertRaises(ValueError): pub.validate_jobs(bad, request())
        for step in range(2):
            for status in ('skipped', 'failure', None):
                bad = jobs(); bad[0]['steps'][step]['conclusion'] = status
                with self.assertRaises(ValueError): pub.validate_jobs(bad, request())
        bad = jobs(); bad[-2]['conclusion'] = 'skipped'
        with self.assertRaises(ValueError): pub.validate_jobs(bad, request())

    def test_artifact_id_attempt_origin_and_digest_bound(self):
        selected = pub.select_artifacts(artifacts(), request())
        self.assertEqual(set(selected), {'linux', 'windows'})
        for key, value in [('expired', True), ('size_in_bytes', True), ('id', 0),
                           ('size_in_bytes', pub.MAX_DOWNLOAD+1), ('digest', None),
                           ('name', 'cef-windows-checkpoint-1'),
                           ('workflow_run', {'id': 52, 'head_sha': 'a'*40, 'head_branch': 'static-engine'})]:
            bad = artifacts(); bad[0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                pub.select_artifacts(bad, request())
        with self.assertRaises(ValueError): pub.select_artifacts(artifacts()+[artifacts()[0]], request())

    def test_pages_do_not_truncate_at_thirty_or_one_hundred(self):
        api = pub.GitHub()
        with patch.object(api, 'api', side_effect=[{'artifacts': list(range(100))}, {'artifacts': [100]}]) as call:
            self.assertEqual(api.pages('actions/runs/51/artifacts', 'artifacts'), list(range(101)))
            self.assertIn('page=2', call.call_args.args[0])

    def test_release_notes_do_not_misstate_static_or_runtime_scope(self):
        text = pub.release_body(request())
        for part in ('NOT an entirely static Linux', 'Sandbox and GPU', 'C API only',
                     'not a new runtime test', 'not checkpoints', 'static CRT'):
            self.assertIn(part, text)
        self.assertIn(request()['producer_commit'], text)


class TransportTests(unittest.TestCase):
    def test_flat_zip_crc_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); archive = root/'transport.zip'
            with zipfile.ZipFile(archive, 'w') as z: z.writestr('bundle.zip', b'bytes')
            pub.extract_transport(archive, root/'bundle')
            self.assertEqual((root/'bundle/bundle.zip').read_bytes(), b'bytes')
            with self.assertRaises(ValueError): pub.extract_transport(archive, root/'bundle')

    def test_traversal_duplicate_symlink_and_zip_bomb_metadata_rejected(self):
        cases = []
        for name in ('../escape', '/absolute', 'sub/file', 'C:device', 'a\\b'):
            cases.append([(zipfile.ZipInfo(name), b'x')])
        symlink = zipfile.ZipInfo('link'); symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases += [[(symlink, b'/outside')], [(zipfile.ZipInfo('a'), b'x'), (zipfile.ZipInfo('A'), b'y')]]
        for case in cases:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp); archive = root/'transport.zip'
                with zipfile.ZipFile(archive, 'w') as z:
                    for info, data in case: z.writestr(info, data)
                with self.assertRaises(ValueError): pub.extract_transport(archive, root/'out')
                self.assertFalse((root/'out').exists())

    def test_download_digest_and_size_fail_before_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp)/'archive.zip'
            def download(*args, **kwargs):
                kwargs['stdout'].write(b'damaged')
                return type('Result', (), {'returncode': 0, 'stderr': b''})()
            with patch.object(pub.subprocess, 'run', side_effect=download), self.assertRaises(ValueError):
                pub.GitHub().download(artifacts()[0], dest)


class FakeGitHub:
    def __init__(self):
        self.ref = None; self.release = None; self.assets = []; self.calls = []
        self.upload_failure = False; self.corrupt_remote = False; self.current_run = run()
    def api(self, endpoint, data=None, method='GET', optional=False):
        self.calls.append((method, endpoint, data))
        if endpoint == 'actions/runs/51': return self.current_run
        if endpoint.startswith('git/ref/tags/'): return self.ref
        if endpoint.startswith('releases/tags/'): return self.release
        if endpoint == 'git/refs' and method == 'POST':
            self.ref = {'object': {'sha': data['sha'], 'type': 'commit',
                'url': f"https://api.github.com/repos/{pub.REPOSITORY}/git/commits/{data['sha']}"}}
            return self.ref
        if endpoint == 'releases' and method == 'POST':
            self.release = dict(data, id=5, html_url='https://github.com/dobord/cef/releases/tag/test')
            return self.release
        if endpoint == 'releases/5' and method == 'PATCH':
            self.release = dict(self.release, **data); return self.release
        raise AssertionError((endpoint, data, method))
    def pages(self, endpoint, field=None):
        if endpoint == 'releases/5/assets': return self.assets
        raise AssertionError(endpoint)
    def upload(self, tag, path):
        if self.upload_failure: raise RuntimeError('simulated network failure')
        row = sdk.file_record(path)
        self.assets.append({'name': row['name'], 'size': row['size'], 'state': 'uploaded',
                            'digest': 'sha256:'+('f'*64 if self.corrupt_remote else row['sha256'])})


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.file = self.root/'asset.zip'; self.file.write_bytes(b'verified fixture')
        self.api = FakeGitHub()
    def tearDown(self): self.temp.cleanup()
    def call(self): return pub.publish(self.api, request(), [self.file], self.root)

    def test_draft_upload_verify_then_publish_idempotently(self):
        result = self.call()
        self.assertEqual(result['status'], 'published')
        self.assertFalse(result['new_runtime_test']); self.assertFalse(result['new_engine_compilation'])
        writes = [c for c in self.api.calls if c[0] != 'GET']
        self.assertEqual([c[0] for c in writes], ['POST', 'POST', 'PATCH'])
        self.assertTrue(writes[1][2]['draft']); self.assertFalse(writes[2][2]['draft'])
        self.assertEqual(writes[1][2]['target_commitish'], 'a'*40)
        self.assertEqual(writes[2][2]['make_latest'], 'false')
        self.api.calls = []; self.call()
        self.assertFalse([c for c in self.api.calls if c[0] != 'GET'])

    def test_upload_failure_keeps_draft_and_retry_adds_missing_without_overwrite(self):
        self.api.upload_failure = True
        with self.assertRaises(RuntimeError): self.call()
        self.assertTrue(self.api.release['draft']); self.assertFalse((self.root/'publication.json').exists())
        self.api.upload_failure = False; self.call()
        self.assertEqual(len(self.api.assets), 1); self.assertFalse(self.api.release['draft'])

    def test_bad_remote_hash_never_publishes(self):
        self.api.corrupt_remote = True
        with self.assertRaisesRegex(ValueError, 'digest'): self.call()
        self.assertTrue(self.api.release['draft'])
        self.assertFalse([c for c in self.api.calls if c[0] == 'PATCH'])

    def test_wrong_tag_cannot_be_retargeted(self):
        self.api.ref = {'object': {'sha': 'c'*40, 'type': 'commit'}}
        with self.assertRaisesRegex(ValueError, 'tag'): self.call()
        self.assertFalse([c for c in self.api.calls if c[0] != 'GET'])

    def test_old_hybrid_draft_or_foreign_body_is_untouched(self):
        self.call(); self.api.release['draft'] = True; self.api.release['body'] = 'old hybrid draft'
        self.api.calls = []
        with self.assertRaisesRegex(ValueError, 'does not belong'): self.call()
        self.assertFalse([c for c in self.api.calls if c[0] != 'GET'])

    def test_unknown_and_missing_published_assets_fail(self):
        self.call(); self.api.assets[0]['name'] = 'unexpected.zip'
        with self.assertRaises(ValueError): self.call()
        self.api.assets = []
        with self.assertRaisesRegex(ValueError, 'missing'): self.call()

    def test_rerun_between_verification_and_publication_has_no_writes(self):
        self.api.current_run['run_attempt'] = 2
        with self.assertRaises(ValueError): self.call()
        self.assertFalse([c for c in self.api.calls if c[0] != 'GET'])


class WorkflowTests(unittest.TestCase):
    def test_publication_is_explicit_and_no_compile_or_wildcard_upload(self):
        root = HERE.parents[1]
        workflow = (root/'.github/workflows/static-sdk-publish.yml').read_text()
        self.assertIn('contents: read', workflow)
        self.assertIn('contents: write', workflow)
        self.assertIn("github.ref == 'refs/heads/static-engine'", workflow)
        self.assertIn("github.event_name != 'pull_request'", workflow)
        self.assertIn('needs: contract', workflow)
        self.assertNotIn('source_build.py', workflow)
        self.assertNotIn('ci.py --triplet', workflow)
        code = (HERE/'publish_sdk.py').read_text()
        self.assertIn('sdk.release_files(bundles, request[\'producer_commit\'])', code)
        self.assertNotIn("'--clobber'", code)

class CompletePromotionTests(unittest.TestCase):
    def test_real_transport_and_sdk_validation_before_fake_release_transaction(self):
        """Real ZIP/CRC/SHA/receipt checks; only GitHub network operations are fake."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); wrappers = {}; records = []
            for i, (platform, triplet) in enumerate(sdk.TRIPLETS.items(), 1):
                source = root/('source-'+platform); source.mkdir()
                count = 3 if platform == 'windows' else 1
                digest = 'd'*64
                receipt = {'schema': 1, 'integration_commit': 'a'*40, 'triplet': triplet,
                    'cef_commit': sdk.CEF, 'chromium_commit': sdk.CHROMIUM, 'vcpkg_commit': sdk.VCPKG,
                    'configuration': 'Release', 'engine_linkage': 'static', 'capi_only': True,
                    'sdk_relocation_verified': True, 'application_relocation_verified': True,
                    'sandbox_verified': False, 'system_libraries_static': False,
                    'executable_sha256': digest,
                    'smoke': {'cef': sdk.VERSION, 'engine': 'static', 'interface': 'capi',
                              'javascript': True, 'paint': True, 'browser_modules_clean': True,
                              'renderer_modules_clean': True, 'browser_pid': 11, 'renderer_pid': 12},
                    'smoke_runs': {'status': 'success', 'engine_runtime_verified': True,
                        'no_retry_on_failure': True, 'required_runs': count, 'passed_runs': count,
                        'executable_sha256': digest,
                        'runs': [{'number': n, 'status': 'success', 'engine_runtime_verified': True,
                                  'cwd': 'synthetic-profile-'+str(n)} for n in range(1, count+1)]}}
                (source/'static-sdk-receipt.json').write_bytes(sdk.json_bytes(receipt))
                (source/'synthetic-not-cef.txt').write_text('Protocol test only; not a CEF SDK')
                name = 'cef-152.0.6-'+triplet+'-static-engine-capi'
                output = root/('transport-'+platform)
                sdk.package_sdk(source, output, name, part_bytes=1024)
                wrapper = root/(platform+'.zip')
                with zipfile.ZipFile(wrapper, 'w') as z:
                    for path in output.iterdir(): z.write(path, path.name)
                record = artifacts()[i-1]
                record.update(size_in_bytes=wrapper.stat().st_size,
                              digest='sha256:'+sdk.hash_file(wrapper))
                records.append(record); wrappers[record['id']] = wrapper
            class Api(FakeGitHub):
                def pages(self, endpoint, field=None):
                    if endpoint == 'actions/runs/51/attempts/1/jobs': return jobs()
                    if endpoint == 'actions/runs/51/artifacts': return records
                    return super().pages(endpoint, field)
                def download(self, artifact, destination):
                    import shutil
                    shutil.copyfile(wrappers[artifact['id']], destination)
            api = Api(); req = root/'request.json'; req.write_bytes(sdk.json_bytes(request()))
            argv = ['publish', '--request', str(req), '--workspace', str(root/'work'),
                    '--proof', str(root/'proof'), '--apply']
            env = {'GITHUB_REPOSITORY': pub.REPOSITORY, 'GITHUB_REF': 'refs/heads/static-engine',
                   'GITHUB_EVENT_NAME': 'push'}
            with patch.object(sys, 'argv', argv), patch.dict(os.environ, env), patch.object(pub, 'GitHub', return_value=api):
                pub.main()
            result = sdk.read_json(root/'proof/publication.json')
            self.assertEqual(result['status'], 'published')
            self.assertGreater(len(result['assets']), 10)  # Both genuine multipart bundles.
            self.assertEqual(len(api.assets), len(result['assets']))


if __name__ == '__main__': unittest.main()
