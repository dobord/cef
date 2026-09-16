"""Reproduce a native startup hang and symbolize the exact diagnostic executable.

Not a CEF SDK validator. The linker map is generated before the executable is
launched, so it never labels old addresses using a different binary's symbols.
"""
from __future__ import annotations
import bisect
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def sha(path: Path) -> str:
    with path.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def main() -> None:
    if os.name != 'nt': raise RuntimeError('Native Windows diagnostic only')
    root = Path.cwd()
    source = Path(os.environ['RUNNER_TEMP'])/'cef-static/download/chromium/src'
    out = source/'out/CEF_Static_Release_x64'
    logs = root/'hang-diagnostics'; logs.mkdir(exist_ok=True)
    exe = out/'cef_static_smoke.exe'
    before = sha(exe)
    if before != '2e75a7ff6b99a700e49f3356955b19b8680985ecf9a15f308586d7fff97c44d0':
        raise RuntimeError('Not the selected failing reference executable')
    ninja = source/'third_party/ninja/ninja.exe'
    commands = subprocess.check_output([str(ninja), '-C', str(out), '-t', 'commands', '-s', exe.name], text=True, timeout=120)
    commands = [line for line in commands.splitlines() if 'lld-link' in line and exe.name in line]
    if len(commands) != 1: raise RuntimeError('Ambiguous final link command')
    map_path = logs/'diagnostic.map'
    command = commands[0] + ' ' + subprocess.list2cmdline(['/MAP:'+str(map_path)])
    with (logs/'map-link.log').open('w', encoding='utf-8') as f:
        subprocess.run(command, shell=True, cwd=out, stdout=f, stderr=subprocess.STDOUT, check=True, timeout=1200)
    binary_hash = sha(exe)
    provenance = {'diagnostic_only': True, 'engine_runtime_verified': False,
                  'input_sha256': before, 'diagnostic_executable_sha256': binary_hash,
                  'map_sha256': sha(map_path), 'map_generated_before_launch': True, 'runs': []}
    def record():
        (logs/'provenance.json').write_text(json.dumps(provenance, indent=2)+'\n', encoding='utf-8')
    record()
    deploy = Path(os.environ['RUNNER_TEMP'])/'cef-hang-probe-deploy'
    if deploy.exists(): raise RuntimeError('Diagnostic deployment must be new')
    deploy.mkdir()
    for path in out.iterdir():
        if path.name == exe.name or path.name == exe.name+'.pdb' or path.suffix in ('.pak', '.bin', '.dat'):
            if path.is_file(): shutil.copy2(path, deploy/path.name)
    if (out/'locales').is_dir(): shutil.copytree(out/'locales', deploy/'locales')
    if sha(deploy/exe.name) != binary_hash: raise RuntimeError('Diagnostic copy changed')
    if not (deploy/'icudtl.dat').is_file(): raise RuntimeError('Missing ICU data')
    failed = False
    try:
        for index in range(1, 6):
            cwd = deploy/f'profile-{index:02d}'; cwd.mkdir()
            folder = logs/f'run-{index:02d}'; folder.mkdir()
            env = dict(os.environ, CHROME_LOG_FILE=str(cwd/'cef-static.log'), _NT_SYMBOL_PATH=str(deploy))
            with (folder/'threads.log').open('w', encoding='utf-8') as f:
                result = subprocess.run([str(root/'hang-diagnostics/probe.exe'), str(deploy/exe.name), '120'],
                                        cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=180)
            row = {'number': index, 'exit_code': result.returncode, 'executable_sha256': sha(deploy/exe.name)}
            for name in ('cef-static.log', 'debug.log', 'smoke-result.json'):
                if (cwd/name).is_file(): shutil.copy2(cwd/name, folder/name)
            if (cwd/'smoke-result.json').exists():
                row['candidate_proof'] = json.loads((cwd/'smoke-result.json').read_text())
            provenance['runs'].append(row); record()
            if result.returncode != 0:
                failed = True; break
    finally:
        frames = []
        for logfile in sorted(logs.glob('run-*/threads.log')):
            text = logfile.read_text(encoding='utf-8')
            match = re.search(r'MODULE (0x[0-9a-f]+) \d+ cef_static_smoke\.exe', text, re.I)
            image_base = int(match[1],16) if match else None
            for line in text.splitlines():
                m = re.match(r'FRAME (\d+) (\d+) (0x[0-9a-f]+) (0x[0-9a-f]+) (0x[0-9a-f]+) (.*)', line)
                if m and int(m[4],16) == image_base:
                    frames.append({'run': logfile.parent.name, 'thread': int(m[1]), 'frame': int(m[2]),
                                   'rva': int(m[5],16), 'dbghelp': m[6]})
        wanted = sorted({f['rva'] for f in frames}); best = {}; preferred = None
        with map_path.open(encoding='utf-8',errors='replace') as f:
            for line in f:
                m = re.search(r'Preferred load address is\s+([0-9a-f]+)', line, re.I)
                if m: preferred = int(m[1],16)
                m = re.match(r'\s*[0-9a-f]{4}:[0-9a-f]+\s+(\S+)\s+([0-9a-f]{16})\s+(.*)',line,re.I)
                if not m or preferred is None: continue
                start = int(m[2],16)-preferred
                i = bisect.bisect_left(wanted,start)
                for address in wanted[i:]:
                    if address not in best or best[address][0] < start:
                        best[address] = (start, m[1]+' '+m[3])
        for frame in frames:
            if frame['rva'] in best:
                start, name = best[frame['rva']]
                frame.update(symbol=name, offset=frame['rva']-start)
        (logs/'resolved-stacks.json').write_text(json.dumps({'diagnostic_only': True,
            'diagnostic_executable_sha256': binary_hash, 'frames': frames},indent=2)+'\n',encoding='utf-8')
        if sha(deploy/exe.name) != binary_hash: raise RuntimeError('Diagnostic executable mutated')
    if failed: raise RuntimeError('Native test failed; see matching-map thread stacks')


if __name__ == '__main__': main()
