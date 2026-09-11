#!/usr/bin/env python3
"""Bounded Windows Ninja iteration. Checkpoint completion is NOT engine success."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkpoint
spec = importlib.util.spec_from_file_location('slice_builder', ROOT/'vcpkg/ports/cef-static/source_build.py')
build = importlib.util.module_from_spec(spec); spec.loader.exec_module(build)


def run_ninja(command: list[str | Path], cwd: Path, logfile: Path,
              seconds: int, grace: int = 120) -> dict:
    """Interrupt Ninja itself, allowing it to clean incomplete edge outputs.

    Hard taskkill is only an emergency fallback. A forced stop never produces
    a usable checkpoint: killed compilers may have left incomplete .obj files.
    """
    if seconds <= 0 or grace <= 0:
        raise ValueError('Positive slice/grace budgets required')
    logfile.parent.mkdir(parents=True, exist_ok=True)
    windows = os.name == 'nt'
    timed_out, unsafe, start = False, False, time.monotonic()
    status = {'status': 'running', 'engine_runtime_verified': False, 'slice_seconds': seconds}
    with logfile.open('w', encoding='utf-8') as log:
        process = subprocess.Popen(list(map(str, command)), cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
            start_new_session=not windows)
        def drain():
            for line in process.stdout:
                log.write(line); log.flush()
                print(line, end='', flush=True)
        reader = threading.Thread(target=drain, daemon=True); reader.start()
        try:
            try:
                process.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.send_signal(signal.CTRL_BREAK_EVENT if windows else signal.SIGINT)
                process.wait(timeout=grace)
        except BaseException:
            unsafe = True
            if process.poll() is None:
                if windows:
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], check=False)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=30)
            raise
        finally:
            reader.join(timeout=30)
            if reader.is_alive():
                unsafe = True
            else:
                process.stdout.close()
            status.update(exit_code=process.poll(), timed_out=timed_out,
                          unsafe_stop=unsafe, elapsed_seconds=round(time.monotonic()-start, 3))
            status['status'] = 'complete' if not unsafe and process.returncode == 0 else 'failed'
            if not unsafe and timed_out and process.returncode != 0:
                text = logfile.read_text(encoding='utf-8')
                if 'interrupted by user' in text and 'FAILED:' not in text:
                    status['status'] = 'checkpoint'
            logfile.with_suffix('.json').write_text(json.dumps(status, indent=2)+'\n')
    print(json.dumps(status, sort_keys=True), flush=True)
    if status['status'] == 'failed':
        raise RuntimeError('Ninja failed or could not stop cleanly; no usable checkpoint: '+str(logfile))
    return status


def main():
    if os.name != 'nt':
        raise ValueError('Production slice is Windows-only')
    work = Path(os.environ.get('CEF_STATIC_WORK') or (Path(os.environ['RUNNER_TEMP'])/'cef-static')).resolve()
    diagnostics = ROOT/'static-diagnostics/iteration'; diagnostics.mkdir(parents=True, exist_ok=True)
    identity = checkpoint.ci_identity(work)
    build.setup_environment(work)
    source = build.prepare(work, diagnostics)
    out = build.configuration(source, diagnostics)
    seconds = build.positive_env('CEF_WINDOWS_SLICE_SECONDS', 10800, 10800)
    jobs = build.positive_env('CEF_STATIC_JOBS', 4, 1024)
    ninja = build.find_binary(source, ['third_party/ninja/ninja.exe'])
    result = run_ninja([ninja, '-C', out, '-j', str(jobs), '-d', 'keeprsp', 'cef_static_smoke'],
                       source, diagnostics/'ninja-slice.log', seconds)
    # Success here means Ninja finished, NOT that a browser/renderer ran.
    # ci.py still goes through vcpkg and must run both native and SDK consumers.
    package = ROOT/'windows-checkpoint'
    saved = checkpoint.save(work, package, identity)
    result.update(checkpoint_files=saved['files'], checkpoint_bytes=sum(p['bytes'] for p in saved['parts']))
    (diagnostics/'iteration.json').write_text(json.dumps(result, indent=2)+'\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('ready='+str(result['status']=='complete').lower()+'\n')
        output.write('checkpoint_ready=true\n')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
        summary.write('## Windows Ninja iteration\n\nStatus: **'+result['status']+'**. '
            'Checkpoint is a resumable build workspace, **not a CEF SDK**. '
            'Native runtime and external vcpkg consumer checks remain mandatory.\n\n')
        if result['status'] == 'checkpoint':
            summary.write('Re-run this workflow at the same commit to continue. '
                'Objects from the last compatible uploaded checkpoint will be restored.\n')
    print('WINDOWS_NINJA_'+result['status'].upper()+'; NO_RUNTIME_SUCCESS_CLAIM', flush=True)


if __name__ == '__main__':
    main()
