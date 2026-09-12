#!/usr/bin/env python3
"""Bounded Ubuntu compilation and full checkpoint; never a fake SDK success."""
from __future__ import annotations
import json
import os
from pathlib import Path
import linux_checkpoint as checkpoint
from windows_slice import build, run_ninja

ROOT = Path(__file__).resolve().parents[2]


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
    audit = checkpoint.shared.preflight(work)
    (diagnostics/'checkpoint-preflight.json').write_text(json.dumps(audit, indent=2)+'\n')
    ninja = build.find_binary(source, ['third_party/ninja/ninja'])
    result = run_ninja([ninja, '-C', out, '-j', str(jobs), '-d', 'keeprsp', 'cef_static_smoke'],
                       source, diagnostics/'ninja-slice.log', seconds)
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
            summary.write('Re-run all jobs at the same commit to continue both platforms. '
                'A successful checkpoint alone does not publish an SDK.\n')
    print('LINUX_NINJA_'+result['status'].upper()+'; NO_RUNTIME_SUCCESS_CLAIM', flush=True)


if __name__ == '__main__':
    main()
