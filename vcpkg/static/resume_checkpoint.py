#!/usr/bin/env python3
"""Strict cross-run continuation, without changing the archive/build recipe.

A retry of a GitHub run can hide its previous attempt's artifacts. Continuation
therefore starts a NEW run and requires the latest attempt of one completed
producer. Never silently substitute an older run or a cold workspace.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import quote

import checkpoint as shared

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = '.github/workflows/static-engine-build.yml'
MAX_PAGES = 10


def positive_id(value: object, label: str) -> int:
    if not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise ValueError(f'{label} must be a positive integer')
    return int(str(value))


def new_run_only(attempt: object) -> None:
    if positive_id(attempt, 'GITHUB_RUN_ATTEMPT') != 1:
        raise ValueError('Do not re-run a checkpoint producer. Start a NEW workflow run '
                         'on this branch and select a completed checkpoint_run. '
                         'Previous-attempt artifacts may no longer be discoverable.')


def scope(identity: dict) -> tuple[str, str]:
    repo, ref = identity['repository'], identity['ref']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/(?!\.\.?$)[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid checkpoint repository')
    if not ref.startswith('refs/heads/') or not ref[len('refs/heads/'):]:
        raise ValueError('Resume requires a branch ref; a new release tag uses fresh mode')
    return repo, ref[len('refs/heads/'):]


def eligible(run: dict, repo: str, branch: str) -> bool:
    return (run.get('path') == WORKFLOW and run.get('event') in ('push', 'workflow_dispatch')
            and run.get('head_repository', {}).get('full_name') == repo
            and run.get('repository', {}).get('full_name') == repo
            and run.get('head_branch') == branch)


def choose_run(identity: dict, current_run: int, requested: str, api=shared.gh_json) -> dict:
    repo, branch = scope(identity)
    if requested:
        run_id = positive_id(requested, 'checkpoint_run')
    else:
        # Decide the producer BEFORE looking at artifacts. Falling through to an
        # older producer when the latest checkpoint disappeared repeats hours of work.
        run_id = None
        for page in range(1, MAX_PAGES + 1):
            response = api(f'repos/{repo}/actions/runs?branch={quote(branch, safe="")}'
                           f'&per_page=100&page={page}')
            rows = response['workflow_runs']
            candidates = [r for r in rows if r['id'] != current_run and eligible(r, repo, branch)]
            if candidates:
                run_id = max(candidates, key=lambda r: (r['created_at'], r['id']))['id']
                break
            if len(rows) < 100:
                break
        if run_id is None:
            raise ValueError('No previous producer found. An intentional first build '
                             'requires workflow_dispatch with checkpoint_mode=fresh.')
    if run_id == current_run:
        raise ValueError('A continuation must not consume its own workflow run')
    run = api(f'repos/{repo}/actions/runs/{run_id}')
    if run.get('id') != run_id or not eligible(run, repo, branch):
        raise ValueError('Checkpoint producer repository, branch, event or workflow is untrusted')
    if run.get('status') != 'completed':
        raise ValueError('Checkpoint producer is still active; refuse an incomplete or changing run')
    positive_id(run.get('run_attempt'), 'producer run_attempt')
    return run


def choose_artifact(identity: dict, run: dict, prefix: str, api=shared.gh_json) -> dict:
    repo, _ = scope(identity)
    expected = f"{prefix}-{run['id']}-{run['run_attempt']}"
    seen_names = []
    matches = []
    for page in range(1, MAX_PAGES + 1):
        rows = api(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100&page={page}")['artifacts']
        for artifact in rows:
            if artifact['name'].startswith('cef-') and 'checkpoint-' in artifact['name']:
                seen_names.append(artifact['name'])
            if artifact['name'] == expected:
                matches.append(artifact)
        if len(rows) < 100:
            break
    else:
        raise ValueError('Artifact listing exceeds bounded pagination; refusing ambiguous selection')
    if len(matches) != 1:
        raise ValueError(f'Required checkpoint {expected} is missing or ambiguous. '
                         'No older checkpoint or fresh build will be substituted. '
                         f'Available checkpoints: {seen_names}')
    artifact = matches[0]
    origin = artifact.get('workflow_run', {})
    if (artifact.get('expired') is not False or origin.get('id') != run['id']
            or origin.get('head_sha') != run['head_sha']
            or origin.get('head_branch') != run['head_branch']
            or type(artifact.get('size_in_bytes')) is not int or artifact['size_in_bytes'] < 1):
        raise ValueError('Checkpoint is expired or its origin/size does not match the producer')
    positive_id(artifact.get('id'), 'artifact id')
    return artifact


def verify_stable(run: dict, repo: str, api=shared.gh_json) -> None:
    current = api(f"repos/{repo}/actions/runs/{run['id']}")
    fields = ('id', 'run_attempt', 'head_sha', 'head_branch', 'status', 'path', 'event')
    stable = all(current.get(k) == run.get(k) for k in fields)
    stable = stable and all(current.get(k, {}).get('full_name') == run.get(k, {}).get('full_name')
                            for k in ('head_repository', 'repository'))
    if not stable:
        raise ValueError('Producer was re-run or changed during download; checkpoint not restored')


def checkpoint_input_identity(identity: dict, run: dict) -> tuple[dict, dict | None]:
    """Only the committed, exact one-step migration may consume an old recipe.

    Work path, image, branch, repository, archive schema and POSIX policy are NOT
    relaxed. Missing artifacts still fail; this is not a search/downgrade loop.
    """
    path = ROOT/'vcpkg/static/checkpoint-migrations.json'
    document = json.loads(path.read_text(encoding='utf-8'))
    if document.get('schema') != 1 or not isinstance(document.get('migrations'), list):
        raise ValueError('Invalid checkpoint migration ledger')
    matches = [m for m in document['migrations'] if
        m['platform'] == identity['platform'] and m['to_recipe'] == identity['recipe']
        and m['producer_run'] == run['id'] and m['producer_sha'] == run['head_sha']
        and m['producer_attempt'] == run['run_attempt']]
    if len(matches) > 1:
        raise ValueError('Ambiguous checkpoint migration')
    if not matches:
        return identity, None
    migration = matches[0]
    if not re.fullmatch(r'[0-9a-f]{64}', migration['from_recipe']):
        raise ValueError('Invalid old recipe digest')
    return dict(identity, recipe=migration['from_recipe']), migration


def restore_selected(adapter, work: Path, package: Path, identity: dict, run: dict,
                     artifact: dict, *, api=shared.gh_json, download=subprocess.run) -> dict:
    if package.exists() or (work.exists() and any(work.iterdir())):
        raise ValueError('Refusing to merge checkpoint download or overwrite a source workspace')
    repo = identity['repository']
    verify_stable(run, repo, api)
    print(f"Restoring run {run['id']}, attempt {run['run_attempt']}, artifact {artifact['id']}", flush=True)
    download(['gh', 'run', 'download', str(run['id']), '--repo', repo,
              '--name', artifact['name'], '--dir', str(package)], check=True, timeout=3600)
    verify_stable(run, repo, api)
    # Existing audited unpacker still checks exact identity, SHA-256 for every
    # part, timestamps, paths, links, file counts and credential exclusions.
    manifest = adapter.restore(package, work, identity)
    shutil.rmtree(package)  # only this invocation's download; never source data
    return {'status': 'restored', 'identity': identity,
            'restored_from_run': run['id'], 'restored_from_attempt': run['run_attempt'],
            'restored_from_sha': run['head_sha'], 'artifact_id': artifact['id'],
            'artifact_name': artifact['name'], 'artifact_digest': artifact.get('digest'),
            'files': manifest['files'], 'unpacked_bytes': manifest['unpacked_bytes'],
            'parts': len(manifest['parts']), 'engine_runtime_verified': False}


def work_path() -> Path:
    return Path(os.environ.get('CEF_STATIC_WORK') or (Path(os.environ['RUNNER_TEMP'])/'cef-static')).resolve()


def adapter_for_host():
    if sys.platform == 'linux':
        import linux_checkpoint
        return linux_checkpoint, 'linux'
    if os.name == 'nt':
        return shared, 'windows'
    raise ValueError('Only native Windows and Linux checkpoint hosts are supported')


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2)+'\n', encoding='utf-8')



def push_request(mode: str, requested: str) -> tuple[str, str, bool]:
    """A committed marker enables explicit continuation without modifying main.

    GitHub may not offer workflow_dispatch when the workflow is absent from the
    default branch. This is an ordinary push trigger, not a CI write-back job.
    """
    if os.environ.get('GITHUB_EVENT_NAME') != 'push':
        return mode, requested, False
    path = ROOT/'vcpkg/static/iteration-request.json'
    request = json.loads(path.read_text(encoding='utf-8'))
    if (request.get('schema') != 1 or type(request.get('sequence')) is not int
            or request['sequence'] < 1 or request.get('mode') not in ('resume', 'fresh')
            or not isinstance(request.get('checkpoint_run'), str)):
        raise ValueError('Invalid committed iteration-request.json')
    per_platform = request.get('checkpoint_runs', {})
    if (not isinstance(per_platform, dict) or set(per_platform) - {'linux', 'windows'}
            or (per_platform and set(per_platform) != {'linux', 'windows'})):
        raise ValueError('checkpoint_runs must pin both native platforms')
    for value in per_platform.values():
        positive_id(value, 'platform checkpoint run')
    if per_platform and (request['checkpoint_run'] or request['mode'] != 'resume'):
        raise ValueError('Ambiguous platform checkpoint request')
    platform = 'windows' if os.name == 'nt' else 'linux'
    mode = request['mode']
    requested = requested or per_platform.get(platform, request['checkpoint_run'])
    return mode, requested, mode == 'fresh'



def restore_main(mode: str, requested: str) -> None:
    new_run_only(os.environ['GITHUB_RUN_ATTEMPT'])
    mode, requested, committed_fresh = push_request(mode, requested)
    adapter, platform = adapter_for_host()
    work = work_path()
    identity = adapter.ci_identity(work)
    diagnostics = ROOT/'static-diagnostics/resume'
    write_json(diagnostics/'selection.json', {'mode': mode, 'checkpoint_run': requested,
                                             'identity': identity, 'status': 'selecting'})
    try:
        if mode == 'fresh':
            if requested or (not committed_fresh and os.environ.get('GITHUB_EVENT_NAME') not in ('workflow_dispatch', 'release')):
                raise ValueError('Fresh mode requires an explicit committed request, manual dispatch or new release, '
                                 'and no checkpoint_run')
            if work.exists() and any(work.iterdir()):
                raise ValueError('Fresh mode cannot erase an existing workspace')
            result = {'status': 'explicit-fresh', 'identity': identity, 'engine_runtime_verified': False}
        else:
            run = choose_run(identity, positive_id(os.environ['GITHUB_RUN_ID'], 'GITHUB_RUN_ID'), requested)
            input_identity, migration = checkpoint_input_identity(identity, run)
            artifact = choose_artifact(input_identity, run, adapter.artifact_name(input_identity))
            write_json(diagnostics/'selection.json', {'status': 'selected', 'mode': mode,
                       'identity': identity, 'producer_run': run['id'], 'producer_attempt': run['run_attempt'],
                       'artifact_id': artifact['id'], 'artifact_name': artifact['name']})
            result = restore_selected(adapter, work, Path(os.environ['RUNNER_TEMP'])/'cef-resume-download',
                                      input_identity, run, artifact)
            if migration is not None:
                result.update(input_identity=input_identity, identity=identity,
                    recipe_migration=migration, source_upgrade_required=True)
        write_json(diagnostics/'restore.json', result)
        # Existing diagnostic consumers keep their established platform paths.
        legacy = 'linux-checkpoint' if platform == 'linux' else 'checkpoint'
        write_json(ROOT/'static-diagnostics'/legacy/'restore.json', result)
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            output.write(f"artifact_name={adapter.artifact_name(identity)}-"
                         f"{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}\n")
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
            summary.write(f"\n### {platform}: checkpoint input\n{json.dumps(result)}\n")
    except Exception as error:
        write_json(diagnostics/'failure.json', {'status': 'failed', 'reason': str(error),
                   'engine_runtime_verified': False})
        raise


def ninja_outputs(work: Path) -> set[str]:
    log = work/'download/chromium/src/out/CEF_Static_Release_x64/.ninja_log'
    if not log.exists():
        return set()
    with log.open(encoding='utf-8') as stream:
        header = stream.readline().strip()
        if not re.fullmatch(r'# ninja log v[0-9]+', header):
            raise ValueError('Unrecognized Ninja log header')
        outputs = set()
        for row in stream:
            if not row.strip():
                continue
            fields = row.rstrip('\n').split('\t')
            if len(fields) != 5 or not fields[3]:
                raise ValueError('Malformed Ninja log; cannot certify iteration progress')
            outputs.add(fields[3])
        return outputs


def progress_report(before: set[str], after: set[str], ready: bool) -> dict:
    new = after - before
    compiled = [n for n in new if n.endswith(('.o', '.obj', '.a', '.lib', '.rlib'))]
    return {'status': 'progress' if new or ready else 'stalled',
            'before_outputs': len(before), 'after_outputs': len(after), 'new_outputs': len(new),
            'new_compiled_outputs': len(compiled), 'removed_outputs': len(before-after),
            'engine_compilation_complete': ready, 'engine_runtime_verified': False,
            'new_output_sample': sorted(new)[:20]}


def audit_main(phase: str) -> None:
    diagnostics = ROOT/'static-diagnostics/resume'
    before_file = diagnostics/'ninja-before.json'
    outputs = ninja_outputs(work_path())
    if phase == 'audit-before':
        write_json(before_file, {'outputs': sorted(outputs)})
        return
    before = set(json.loads(before_file.read_text(encoding='utf-8'))['outputs'])
    ready = os.environ.get('CEF_ITERATION_READY')
    if ready not in ('true', 'false'):
        raise ValueError('CEF_ITERATION_READY must be true or false')
    report = progress_report(before, outputs, ready == 'true')
    write_json(diagnostics/'progress.json', report)
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
        summary.write('\n### Ninja progress (not a tested SDK)\n'+json.dumps(report)+'\n')
    print(json.dumps(report), flush=True)
    if report['status'] == 'stalled':
        raise ValueError('No additional Ninja outputs completed. Checkpoint retained, '
                         'but another identical iteration must not be reported as progress.')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['guard', 'restore', 'audit-before', 'audit-after'])
    parser.add_argument('--mode', choices=['resume', 'fresh'],
                        default=os.environ.get('CEF_CHECKPOINT_MODE', 'resume'))
    parser.add_argument('--checkpoint-run', default=os.environ.get('CEF_CHECKPOINT_RUN', ''))
    args = parser.parse_args()
    if args.phase == 'guard':
        new_run_only(os.environ['GITHUB_RUN_ATTEMPT'])
    elif args.phase == 'restore':
        restore_main(args.mode, args.checkpoint_run)
    else:
        audit_main(args.phase)


if __name__ == '__main__':
    main()
