#!/usr/bin/env python3
"""Bounded Ubuntu compilation and full checkpoint; never a fake SDK success."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
import linux_checkpoint as checkpoint
from windows_slice import build, run_ninja

ROOT = Path(__file__).resolve().parents[2]


def clean_failed_objects(out: Path, diagnostics: Path) -> dict:
    status = json.loads((diagnostics/'ninja-slice.json').read_text())
    text = (diagnostics/'ninja-slice.log').read_text(encoding='utf-8')
    if (status.get('status') != 'failed' or status.get('unsafe_stop') is not False
            or status.get('timed_out') is not False or status.get('exit_code') not in (1, 2)
            or 'ninja: build stopped: subcommand failed.' not in text):
        raise RuntimeError('Unsafe/unknown stop: checkpoint not permitted')
    failures = re.findall(r'^FAILED: (.+)$', text, re.M)
    if not failures:
        raise RuntimeError('No explicit failed compiler output; checkpoint not permitted')
    paths = []
    for failure in failures:
        # Newer Ninja annotates failed edges; pinned older Ninja omits this.
        name = re.sub(r'^\[code=[12]\] ', '', failure.strip())
        # Do not guess linker/multi-output commands or parse shell-quoted paths.
        if not re.fullmatch(r'obj/[A-Za-z0-9_./+-]+\.o', name):
            raise RuntimeError('Unsupported failed edge; checkpoint not permitted: '+name)
        if any(part in ('', '.', '..') for part in name.split('/')):
            raise RuntimeError('Unsafe failed edge path')
        path = out / name
        if path.is_symlink() or not path.resolve().is_relative_to(out.resolve()):
            raise RuntimeError('Failed edge leaves build directory')
        paths.append(path)
    for path in paths:
        path.unlink(missing_ok=True)
    status.update(status='compile-failed-checkpoint', failed_outputs_removed=[
        p.relative_to(out).as_posix() for p in paths], engine_runtime_verified=False)
    return status


def main() -> None:
    checkpoint.require_linux()
    # Validate limits before source access. Leave the rest of the 350-minute job
    # for sync/restore, compression, upload and eventual external SDK validation.
    seconds = build.positive_env('CEF_LINUX_SLICE_SECONDS', 10800, 10800)
    jobs = build.positive_env('CEF_STATIC_JOBS', 4, 1024)
    work = Path(os.environ.get('CEF_STATIC_WORK') or
                (Path(os.environ['RUNNER_TEMP'])/'cef-static')).resolve()
    diagnostics = ROOT/'static-diagnostics/linux-iteration'
    diagnostics.mkdir(parents=True, exist_ok=True)
    identity = checkpoint.ci_identity(work)
    build.setup_environment(work)
    source = build.prepare(work, diagnostics)
    out = build.configuration(source, diagnostics)
    audit = checkpoint.preflight(work)
    (diagnostics/'checkpoint-preflight.json').write_text(json.dumps(audit, indent=2)+'\n')
    ninja = build.find_binary(source, ['third_party/ninja/ninja'])
    failure = None
    try:
        result = run_ninja([ninja, '-C', out, '-j', str(jobs), '-d', 'keeprsp', 'cef_static_smoke'],
                           source, diagnostics/'ninja-slice.log', seconds)
    except RuntimeError as error:
        result = clean_failed_objects(out, diagnostics)
        failure = error
    # Save before any vcpkg exporter/consumer runs. Even a complete executable
    # is not runtime-verified until ci.py checks the browser and renderer.
    saved = checkpoint.save(work, ROOT/'linux-checkpoint', identity)
    result.update(checkpoint_files=saved['files'], checkpoint_links=len(saved['links']),
                  checkpoint_omitted_files=saved['omitted_files'],
                  checkpoint_bytes=sum(p['bytes'] for p in saved['parts']))
    (diagnostics/'iteration.json').write_text(json.dumps(result, indent=2)+'\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('ready='+str(result['status'] == 'complete').lower()+'\n')
        output.write('checkpoint_ready=true\n')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
        summary.write('## Ubuntu Ninja iteration\n\nStatus: **'+result['status']+'**. '
            'Full source/generated/object checkpoint, **not a CEF SDK**. '
            'Native runtime and relocated vcpkg consumer checks remain mandatory.\n\n')
        if result['status'] == 'checkpoint':
            summary.write('Start a NEW run from this completed checkpoint producer. '
                'A successful checkpoint alone does not publish an SDK.\n')
    print('LINUX_NINJA_'+result['status'].upper()+'; NO_RUNTIME_SUCCESS_CLAIM', flush=True)
    if failure is not None:
        raise RuntimeError('Compiler failure preserved as a checkpoint, NOT a successful build') from failure


if __name__ == '__main__':
    main()
