"""Supervise native CEF smoke tests; diagnostics never turn a failure into success."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Callable

VERSION = '152.0.6+g708dc14+chromium-152.0.7977.83'
TIMEOUT_SECONDS = 120
WINDOWS_RUNS = 3


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2)+'\n', encoding='utf-8')


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def capture_process(pid: int, logs: Path, windows: bool) -> None:
    """Bounded metadata for this test's process tree, never command lines or dumps."""
    if type(pid) is not int or pid <= 0:
        raise ValueError('A positive test process ID is required')
    if windows:
        powershell = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = r'''
$ErrorActionPreference = 'Stop'
$rootId = ROOT_PID
$rows = @(Get-CimInstance -Query 'SELECT ProcessId, ParentProcessId, Name FROM Win32_Process')
$ids = @($rootId)
for ($round = 0; $round -lt 32; $round++) {
  $next = @($rows | Where-Object { $ids -contains $_.ParentProcessId -and $ids -notcontains $_.ProcessId } | ForEach-Object { [int]$_.ProcessId })
  if ($next.Count -eq 0) { break }
  $ids += $next
}
$report = @($rows | Where-Object { $ids -contains $_.ProcessId } | ForEach-Object {
  $row = $_
  try {
    $p = Get-Process -Id $row.ProcessId -ErrorAction Stop
    $threads = @($p.Threads | Select-Object -First 128 | ForEach-Object {
      $reason = $null
      try { if ($_.ThreadState -eq 'Wait') { $reason = [string]$_.WaitReason } } catch {}
      @{ id = $_.Id; state = [string]$_.ThreadState; wait_reason = $reason }
    })
    @{ pid = $row.ProcessId; parent_pid = $row.ParentProcessId; name = $row.Name;
       session_id = $p.SessionId; working_set = $p.WorkingSet64; threads = $threads }
  } catch { @{ pid = $row.ProcessId; name = $row.Name; exited_during_snapshot = $true } }
})
@{ root_pid = $rootId; processes = $report; diagnostic_only = $true } | ConvertTo-Json -Depth 6 -Compress
'''.replace('ROOT_PID', str(pid))
        result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, text=True, timeout=12, check=True)
        report = json.loads(result.stdout.lstrip('\ufeff'))
    else:
        ids, report = [pid], {'root_pid': pid, 'processes': [], 'diagnostic_only': True}
        for current in ids:
            proc = Path('/proc')/str(current)
            try:
                row = {'pid': current, 'status': (proc/'status').read_text(), 'threads': []}
                for task in sorted((proc/'task').iterdir())[:128]:
                    row['threads'].append({'tid': int(task.name), 'wait_channel': (task/'wchan').read_text().strip()})
                report['processes'].append(row)
                for child in (proc/'task'/str(current)/'children').read_text().split():
                    child_id = int(child)
                    if child_id not in ids and len(ids) < 256:
                        ids.append(child_id)
            except (FileNotFoundError, ProcessLookupError):
                report['processes'].append({'pid': current, 'exited_during_snapshot': True})
    write_json(logs/'timeout-processes.json', report)


def run_once(command: list[str | Path], cwd: Path, logs: Path, *, windows: bool,
             terminate: Callable[[subprocess.Popen], None], timeout: int = TIMEOUT_SECONDS) -> None:
    """Use a regular output file, not a pipe held open by renderer descendants."""
    logs.mkdir(parents=True, exist_ok=True)
    args = list(map(str, command))
    status = {'schema': 1, 'name': 'static-smoke', 'command': args, 'cwd': str(cwd),
              'timeout_seconds': timeout, 'status': 'starting',
              'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'engine_runtime_verified': False}
    started = time.monotonic()
    process = None
    write_json(logs/'static-smoke-status.json', status)
    try:
        # A child can inherit stdout, but cannot keep a parent-side pipe reader
        # blocked. Preserve bytes even if the process exits abnormally.
        with (logs/'static-smoke.log').open('wb') as output:
            env = dict(os.environ, CHROME_LOG_FILE=str(cwd/'cef-static.log'))
            process = subprocess.Popen(args, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                       env=env, start_new_session=not windows)
            status.update(pid=process.pid, status='running')
            write_json(logs/'static-smoke-status.json', status)
            try:
                code = process.wait(timeout=timeout)
                status['status'] = 'success' if code == 0 else 'failed'
            except subprocess.TimeoutExpired:
                status['status'] = 'timed_out'
                # Capture before killing. A collector failure must neither mask
                # the original timeout nor prevent process-tree termination.
                try:
                    capture_process(process.pid, logs, windows)
                except Exception as error:
                    write_json(logs/'timeout-capture-error.json', {'error': str(error), 'diagnostic_only': True})
                finally:
                    terminate(process)
                raise RuntimeError(f'Static CEF smoke timed out after {timeout}s; see {logs}')
            except BaseException:
                status['status'] = 'interrupted'
                terminate(process)
                raise
            if code != 0:
                raise RuntimeError(f'Static CEF smoke exited {code}; see {logs}')
    except BaseException:
        if status['status'] == 'starting':
            status['status'] = 'spawn_failed'
        raise
    finally:
        status['elapsed_seconds'] = round(time.monotonic()-started, 3)
        status['exit_code'] = process.poll() if process is not None else None
        write_json(logs/'static-smoke-status.json', status)


def validate_proof(proof: dict) -> None:
    required = ('javascript', 'paint', 'browser_modules_clean', 'renderer_modules_clean')
    if proof.get('cef') != VERSION or proof.get('engine') != 'static':
        raise RuntimeError('Static engine version/linkage does not match the source pin')
    if not all(proof.get(key) is True for key in required):
        raise RuntimeError('Incomplete static engine runtime proof')
    for key in ('browser_pid', 'renderer_pid'):
        if type(proof.get(key)) is not int or proof[key] <= 0:
            raise RuntimeError('Invalid native process proof')
    if proof['browser_pid'] == proof['renderer_pid']:
        raise RuntimeError('A real separate renderer was not observed')


def execute(exe: Path, logs: Path, *, windows: bool,
            terminate: Callable[[subprocess.Popen], None]) -> dict:
    """Require ALL fresh-profile runs, stopping at the first failure (no retries)."""
    exe, logs = exe.resolve(), logs.resolve()
    logs.mkdir(parents=True, exist_ok=True)
    (logs/'smoke-result.json').unlink(missing_ok=True)
    if not exe.is_file():
        raise FileNotFoundError(exe)
    original_hash = digest(exe)
    evidence = Path(tempfile.mkdtemp(prefix='smoke-attempts-', dir=logs))
    count = WINDOWS_RUNS if windows else 1
    report = {'schema': 1, 'required_runs': count, 'passed_runs': 0, 'runs': [],
              'executable_sha256': original_hash, 'engine_runtime_verified': False,
              'runner_image': os.environ.get('ImageVersion'), 'status': 'running',
              'no_retry_on_failure': True}
    receipt = logs/'smoke-runs.json'
    write_json(receipt, report)
    proof = None
    try:
        for index in range(1, count+1):
            attempt_logs = evidence/f'{index:02d}'
            attempt_logs.mkdir()
            # smoke.c derives its cache from cwd. Every repetition receives a
            # genuinely new cwd/profile while resources remain beside the EXE.
            work = Path(tempfile.mkdtemp(prefix='cef-smoke-', dir=exe.parent))
            row = {'number': index, 'cwd': str(work), 'logs': str(attempt_logs.relative_to(logs)),
                   'status': 'running', 'engine_runtime_verified': False}
            report['runs'].append(row)
            write_json(receipt, report)
            command = [exe, '--enable-logging', '--log-level=0', f'--log-file={work / "cef-static.log"}']
            if not windows:
                command = ['xvfb-run', '-a', '-s', '-screen 0 1280x1024x24', *command]
            try:
                run_once(command, work, attempt_logs, windows=windows, terminate=terminate)
                proof = json.loads((work/'smoke-result.json').read_text(encoding='utf-8'))
                validate_proof(proof)
                if digest(exe) != original_hash:
                    raise RuntimeError('Native executable changed during runtime verification')
                row['status'] = 'success'
                row['engine_runtime_verified'] = True
                report['passed_runs'] += 1
            except BaseException:
                row['status'] = 'failed'
                raise
            finally:
                # A candidate proof from a crashed/hung process is evidence,
                # never the top-level successful smoke receipt.
                for name in ('cef-static.log', 'debug.log', 'chrome_debug.log', 'smoke-result.json'):
                    path = work/name
                    if path.is_file() and not path.is_symlink():
                        destination = 'candidate-smoke-result.json' if name == 'smoke-result.json' else name
                        shutil.copy2(path, attempt_logs/destination)
                for name in ('static-smoke.log', 'static-smoke-status.json'):
                    if (attempt_logs/name).is_file():
                        shutil.copy2(attempt_logs/name, logs/name)
                write_json(receipt, report)
            # Removal failure (e.g. surviving children hold profile files) is
            # not ignored. Never delete any pre-existing application profile.
            shutil.rmtree(work)
        assert proof is not None
        report.update(status='success', engine_runtime_verified=True)
        write_json(receipt, report)
        write_json(logs/'smoke-result.json', proof)
        return proof
    except BaseException:
        report.update(status='failed', engine_runtime_verified=False)
        write_json(receipt, report)
        (logs/'smoke-result.json').unlink(missing_ok=True)
        raise
