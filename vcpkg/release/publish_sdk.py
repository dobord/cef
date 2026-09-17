#!/usr/bin/env python3
"""Promote an explicitly selected successful SDK run, never rebuild Chromium.

Only prereleases are created. Downloaded helpers/binaries are never executed.
A complete, verified draft is published only after all remote assets match.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from urllib.parse import quote
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'static'))
import sdk_package as sdk

REPOSITORY = 'dobord/cef'
WORKFLOW = '.github/workflows/static-engine-build.yml'
MAX_DOWNLOAD = 32 * 1024**3
RUNTIME_STEP = 'Build through vcpkg and verify native and relocated SDK consumers'
UPLOAD_STEP = 'Preserve SDK only after successful source and external-consumer tests'


def require(value, message):
    if not value:
        raise ValueError(message)


def positive(value):
    require(type(value) is int and value > 0, 'Expected positive integer')
    return value


def request_data(data):
    require(set(data) == {'schema', 'sequence', 'producer_run', 'producer_attempt',
                         'producer_commit', 'producer_branch', 'tag', 'prerelease'},
            'Unexpected publication request fields')
    require(type(data['schema']) is int and data['schema'] == 1, 'Unsupported request schema')
    for key in ('sequence', 'producer_run', 'producer_attempt'):
        positive(data[key])
    require(isinstance(data['producer_commit'], str) and
            re.fullmatch(r'[0-9a-f]{40}', data['producer_commit']), 'Invalid producer commit')
    require(data['producer_branch'] in ('static-engine', 'vcpkg'), 'Untrusted producer branch')
    require(data['prerelease'] is True, 'This promoter must not publish a stable release')
    require(data['tag'] == f"cef-152.0.6-static-capi-r{data['producer_run']}-a{data['producer_attempt']}",
            'Release tag must uniquely identify the tested run and attempt')
    return dict(data)


def validate_run(run, request):
    require(run.get('id') == request['producer_run'] and
            run.get('run_attempt') == request['producer_attempt'] and
            run.get('head_sha') == request['producer_commit'] and
            run.get('head_branch') == request['producer_branch'], 'Producer identity changed')
    require(run.get('status') == 'completed' and run.get('conclusion') == 'success',
            'Producer has not completed successfully')
    require(run.get('path') == WORKFLOW and run.get('event') in ('push', 'workflow_dispatch'),
            'Untrusted producer workflow or event')
    require(all(run.get(k, {}).get('full_name') == REPOSITORY
                for k in ('repository', 'head_repository')), 'Foreign producer repository')


def validate_jobs(jobs, request):
    require(jobs and all(j.get('run_id') == request['producer_run'] and
                        j.get('run_attempt') == request['producer_attempt'] and
                        j.get('head_sha') == request['producer_commit'] for j in jobs),
            'Foreign or stale job attempt')
    require(all(j.get('status') == 'completed' and j.get('conclusion') in ('success', 'skipped')
                for j in jobs), 'Incomplete or failed producer job')
    for triplet in sdk.TRIPLETS.values():
        selected = [j for j in jobs if j.get('name', '').startswith('source-build (')
                    and j['name'].endswith(', '+triplet+')')]
        require(len(selected) == 1 and selected[0]['conclusion'] == 'success',
                'Exactly one successful source-build job required for '+triplet)
        for step in (RUNTIME_STEP, UPLOAD_STEP):
            matches = [s for s in selected[0].get('steps', []) if s.get('name') == step]
            require(len(matches) == 1 and matches[0].get('conclusion') == 'success',
                    'Missing successful native validation/upload: '+triplet)
    ready = [j for j in jobs if j.get('name') == 'sdk-readiness']
    require(len(ready) == 1 and ready[0].get('conclusion') == 'success', 'SDK readiness not successful')


def select_artifacts(rows, request):
    selected = {}
    for platform, triplet in sdk.TRIPLETS.items():
        name = f"cef-static-{triplet}-sdk-{request['producer_attempt']}"
        matches = [a for a in rows if a.get('name') == name]
        require(len(matches) == 1, 'Missing or ambiguous SDK artifact: '+name)
        item = matches[0]
        positive(item.get('id'))
        require(item.get('expired') is False and type(item.get('size_in_bytes')) is int and
                0 < item['size_in_bytes'] <= MAX_DOWNLOAD, 'Expired or oversized SDK artifact')
        origin = item.get('workflow_run', {})
        require(origin.get('id') == request['producer_run'] and
                origin.get('head_sha') == request['producer_commit'] and
                origin.get('head_branch') == request['producer_branch'], 'Artifact provenance mismatch')
        require(isinstance(item.get('digest'), str) and
                re.fullmatch(r'sha256:[0-9a-f]{64}', item['digest']), 'Missing artifact digest')
        selected[platform] = item
    return selected


class GitHub:
    """All calls are scoped to the selected repository; never log credentials."""
    def api(self, endpoint, data=None, method='GET', optional=False):
        args = ['gh', 'api', '--method', method, f'repos/{REPOSITORY}/'+endpoint]
        if data is not None:
            args += ['--input', '-']
        result = subprocess.run(args, input=None if data is None else json.dumps(data),
                                text=True, capture_output=True, timeout=180)
        if result.returncode:
            if optional and '(HTTP 404)' in result.stderr:
                return None
            raise RuntimeError(f'GitHub API {method} {endpoint} failed: '+result.stderr[-1000:])
        return json.loads(result.stdout) if result.stdout.strip() else None

    def pages(self, endpoint, field=None):
        result = []
        for page in range(1, 21):
            response = self.api(endpoint+('&' if '?' in endpoint else '?')+
                                f'per_page=100&page={page}')
            rows = response[field] if field else response
            require(isinstance(rows, list), 'Invalid paginated API response')
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise ValueError('API listing exceeds bounded pagination')

    def download(self, artifact, destination):
        with destination.open('xb') as out:
            result = subprocess.run(['gh', 'api',
                f"repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"],
                stdout=out, stderr=subprocess.PIPE, timeout=1800)
        require(result.returncode == 0, 'Artifact download failed: '+result.stderr.decode(errors='replace')[-800:])
        require(destination.stat().st_size == artifact['size_in_bytes'] and
                'sha256:'+sdk.hash_file(destination) == artifact['digest'],
                'Downloaded artifact size/SHA-256 differs from GitHub metadata')

    def upload(self, tag, path):
        subprocess.run(['gh', 'release', 'upload', tag, str(path), '--repo', REPOSITORY],
                       check=True, timeout=1800)  # No --clobber, shell or wildcards.


def extract_transport(archive, destination):
    """Extract only a bounded flat transport directory, not the SDK payload."""
    require(not destination.exists(), 'Refusing to merge an artifact directory')
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        require(0 < len(entries) <= 300, 'Unexpected transport member count')
        names = set()
        for info in entries:
            sdk.safe_name(info.filename)
            require(not info.is_dir() and not info.flag_bits & 1 and
                    stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG),
                    'Special/encrypted transport member')
            require(info.filename.casefold() not in names, 'Duplicate/colliding transport member')
            names.add(info.filename.casefold())
            require(0 < info.file_size < sdk.ASSET_LIMIT, 'Oversized/empty transport member')
        require(sum(i.file_size for i in entries) <= MAX_DOWNLOAD, 'Oversized expanded transport')
        destination.mkdir(parents=True)
        for info in entries:
            with source.open(info) as src, (destination/info.filename).open('xb') as dest:
                shutil.copyfileobj(src, dest, sdk.BLOCK)  # Reading to EOF checks CRC.


def marker(request):
    return f"<!-- cef-static-sdk:{request['producer_run']}:{request['producer_attempt']}:{request['producer_commit']} -->"


def release_body(request):
    return f"""{marker(request)}
# Verified static CEF engine SDKs — C API, Release, x64

CEF 152.0.6 / Chromium 152.0.7977.83.
Tested source commit: `{request['producer_commit']}`.
Producer: https://github.com/{REPOSITORY}/actions/runs/{request['producer_run']} (attempt {request['producer_attempt']}).

These are the original verified Windows and Linux vcpkg SDK artifacts, not checkpoints.
No Chromium rebuild or binary modification was performed during promotion.
The transported archives and every ZIP member are verified before uploading.
The tag points to the TESTED producer commit, not the later publication-script commit.

Windows uses static CEF and the static CRT. System Windows DLLs are still required.
Linux uses a static CEF engine but dynamically linked system libraries (including libc,
GLib, NSS, X11 and ALSA). This is NOT an entirely static Linux executable.
C API only; Release configuration only. Sandbox and GPU functionality are NOT certified.
The off-screen smoke page is controlled test content; do not copy its no-sandbox settings
for untrusted content. This prerelease is deliberately not marked as the latest stable release.

Validation in the producer includes a relocated SDK consumer, distinct browser/renderer,
JavaScript, an expected rendered pixel, and module/import checks. Windows requires three
fresh successful runs, Linux one. Publication verifies those receipts; it is not a new runtime test.

Download all files for the chosen platform. The `*-README.txt` and manifest describe
verification with Python 3.11+ and assembly when `.zip.partNNN` files are present.
A single `.zip` is already a complete ZIP64 SDK. Keep its manifest and receipt for provenance.
"""


def asset_rows(files):
    rows = [sdk.file_record(p) for p in files]
    require(len({r['name'] for r in rows}) == len(rows), 'Release asset collision')
    return {r['name']: r for r in rows}


def check_remote_assets(rows, expected, complete=False):
    seen = set()
    for asset in rows:
        name = asset.get('name')
        require(name in expected and name not in seen, 'Unexpected/duplicate remote release asset')
        seen.add(name)
        value = expected[name]
        require(asset.get('state') == 'uploaded' and asset.get('size') == value['size'] and
                asset.get('digest') == 'sha256:'+value['sha256'], 'Remote asset digest/size mismatch: '+name)
    if complete:
        require(seen == set(expected), 'Release is missing verified assets')
    return seen


def publish(api, request, files, proof):
    tag = request['tag']; encoded = quote(tag, safe=''); body = release_body(request)
    title = 'CEF 152.0.6 static engine C API — verified x64 SDKs'
    expected = asset_rows(files)
    # The producer is rechecked after download and immediately before any write.
    validate_run(api.api(f"actions/runs/{request['producer_run']}"), request)
    ref = api.api('git/ref/tags/'+encoded, optional=True)
    if ref is not None:
        require(ref.get('object', {}) == {'sha': request['producer_commit'], 'type': 'commit',
            'url': f"https://api.github.com/repos/{REPOSITORY}/git/commits/{request['producer_commit']}"},
            'Existing release tag does not point directly to tested commit')
    release = api.api('releases/tags/'+encoded, optional=True)
    if release is not None:
        require(ref is not None and release.get('tag_name') == tag and
                release.get('target_commitish') == request['producer_commit'] and
                release.get('prerelease') is True and release.get('body') == body and
                release.get('name') == title, 'Existing release does not belong to this verified promotion')
    if ref is None:
        api.api('git/refs', {'ref': 'refs/tags/'+tag, 'sha': request['producer_commit']}, 'POST')
    if release is None:
        release = api.api('releases', {'tag_name': tag, 'target_commitish': request['producer_commit'],
                        'name': title, 'body': body, 'draft': True, 'prerelease': True,
                        'make_latest': 'false'}, 'POST')
    release_id = positive(release['id'])
    existing = check_remote_assets(api.pages(f'releases/{release_id}/assets'), expected,
                                   complete=release.get('draft') is not True)
    if release.get('draft') is True:
        for path in files:
            if path.name not in existing:
                # Reject a local change since verification; never overwrite remote assets.
                require(sdk.file_record(path) == expected[path.name], 'Local asset changed before upload')
                print('Uploading verified asset: '+path.name, flush=True)
                api.upload(tag, path)
        check_remote_assets(api.pages(f'releases/{release_id}/assets'), expected, complete=True)
        validate_run(api.api(f"actions/runs/{request['producer_run']}"), request)
        ref = api.api('git/ref/tags/'+encoded)
        require(ref['object']['type'] == 'commit' and ref['object']['sha'] == request['producer_commit'],
                'Release tag moved during upload')
        release = api.api(f'releases/{release_id}', {'draft': False, 'prerelease': True,
                                                   'make_latest': 'false'}, 'PATCH')
    require(release.get('draft') is False and release.get('prerelease') is True,
            'GitHub did not publish the verified prerelease')
    check_remote_assets(api.pages(f'releases/{release_id}/assets'), expected, complete=True)
    result = {'schema': 1, 'status': 'published', 'request': request, 'release_id': release_id,
              'url': release['html_url'], 'assets': list(expected.values()),
              'producer_rechecked': True, 'remote_digests_verified': True,
              'new_engine_compilation': False, 'new_runtime_test': False}
    (proof/'publication.json').write_bytes(sdk.json_bytes(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--proof', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='Create/fill a draft, verify it, then publish a prerelease')
    args = parser.parse_args()
    require(os.environ.get('GITHUB_REPOSITORY') == REPOSITORY, 'Publication restricted to the owning repository')
    require(os.environ.get('GITHUB_REF') == 'refs/heads/static-engine' and
            os.environ.get('GITHUB_EVENT_NAME') in ('push', 'workflow_dispatch'), 'Untrusted publisher ref/event')
    request = request_data(sdk.read_json(args.request)); api = GitHub()
    require(not args.workspace.exists(), 'Publication workspace already exists')
    args.workspace.mkdir(parents=True); args.proof.mkdir(parents=True, exist_ok=True)
    try:
        run = api.api(f"actions/runs/{request['producer_run']}"); validate_run(run, request)
        jobs = api.pages(f"actions/runs/{request['producer_run']}/attempts/{request['producer_attempt']}/jobs", 'jobs')
        validate_jobs(jobs, request)
        selected = select_artifacts(api.pages(f"actions/runs/{request['producer_run']}/artifacts", 'artifacts'), request)
        (args.proof/'producer.json').write_bytes(sdk.json_bytes({'request': request, 'run': run,
                                                              'jobs': jobs, 'artifacts': selected}))
        bundles = args.workspace/'bundles'; bundles.mkdir()
        for platform, artifact in selected.items():
            archive = args.workspace/(platform+'.zip')
            print(f"Downloading {platform} SDK artifact {artifact['id']}", flush=True)
            api.download(artifact, archive)
            extract_transport(archive, bundles/platform)
            archive.unlink()  # Only this invocation's verified transport wrapper.
        validate_run(api.api(f"actions/runs/{request['producer_run']}"), request)
        after = select_artifacts(api.pages(f"actions/runs/{request['producer_run']}/artifacts", 'artifacts'), request)
        require(all(all(after[p].get(k) == selected[p].get(k) for k in
                        ('id', 'name', 'digest', 'size_in_bytes', 'expired')) for p in selected),
                'Producer artifacts changed during download')
        print('Verifying both SDK manifests, every ZIP member and native runtime receipts', flush=True)
        files = sdk.release_files(bundles, request['producer_commit'])
        (args.proof/'verification.json').write_bytes(sdk.json_bytes({'status': 'verified',
            'request': request, 'assets': list(asset_rows(files).values()),
            'artifact_digests_verified': True, 'all_sdk_members_verified': True,
            'runtime_receipts_verified': True, 'new_runtime_test': False}))
        if args.apply:
            result = publish(api, request, files, args.proof)
            print(result['url'], flush=True)
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
                    summary.write('## Verified SDK prerelease\n'+result['url']+'\n\n'
                        +'Promoted original artifacts from run '+str(request['producer_run'])
                        +'; no engine rebuild. Windows and Linux SDKs are attached.\n')
    except Exception as error:
        (args.proof/'failure.json').write_bytes(sdk.json_bytes({'status': 'failed',
            'error': type(error).__name__+': '+str(error), 'request': request}))
        raise


if __name__ == '__main__':
    main()
