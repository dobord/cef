#!/usr/bin/env python3
"""Build and test the explicitly hybrid baseline; never certify a static engine."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
LOCK = json.loads((ROOT / 'vcpkg/cef-source.lock.json').read_text())
WINDOWS = os.name == 'nt'


def run(args: list[str | Path], cwd: Path | None = None, timeout: int = 1800) -> str:
    command = [str(a) for a in args]
    print('+', subprocess.list2cmdline(command), flush=True)
    result = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout, check=False)
    print(result.stdout, flush=True)
    if result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}): {command}')
    return result.stdout


def smoke(executable: Path, out: Path, label: str) -> dict:
    command = [str(executable)]
    if not WINDOWS:
        command = ['xvfb-run', '-a', '-s', '-screen 0 1280x1024x24', *command]
    log_path = out / f'smoke-{label}.log'
    with log_path.open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=executable.parent, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=not WINDOWS)
        try:
            code = process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            if WINDOWS:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], check=False)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise RuntimeError('Smoke test timeout; process tree terminated')
    print(log_path.read_text(encoding='utf-8', errors='replace'), flush=True)
    if code != 0:
        raise RuntimeError(f'{label}: smoke exit code {code}')
    proof = json.loads((executable.parent / 'smoke-result.json').read_text())
    assert proof['javascript'] and proof['paint']
    assert proof['browser_pid'] != proof['renderer_pid']
    assert proof['cef'] == LOCK['cef_version']
    assert proof['engine'] == 'shared' and proof['wrapper'] == 'static'
    (out / f'smoke-{label}.json').write_text(json.dumps(proof, indent=2) + '\n')
    return proof


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--triplet', required=True, choices=['x64-windows-static', 'x64-linux'])
    options = parser.parse_args()
    triplet = options.triplet
    assert WINDOWS == (triplet == 'x64-windows-static')
    work = Path(os.environ.get('RUNNER_TEMP', str(ROOT / '.work'))) / 'cef-vcpkg'
    work.mkdir(parents=True, exist_ok=True)
    out = ROOT / 'artifacts'
    out.mkdir(exist_ok=True)
    vcpkg = work / 'v'
    run(['git', 'init', vcpkg])
    run(['git', 'fetch', '--depth=1', 'https://github.com/microsoft/vcpkg.git', LOCK['vcpkg_commit']], vcpkg)
    run(['git', 'checkout', '--detach', 'FETCH_HEAD'], vcpkg)
    if WINDOWS:
        run(['cmd', '/c', 'bootstrap-vcpkg.bat', '-disableMetrics'], vcpkg)
    else:
        run(['bash', 'bootstrap-vcpkg.sh', '-disableMetrics'], vcpkg)
    binary = vcpkg / ('vcpkg.exe' if WINDOWS else 'vcpkg')
    spec = f'cef:{triplet}'
    common = ['--classic', f'--overlay-ports={ROOT / "vcpkg/ports"}']
    run([binary, 'install', spec, *common, '--clean-after-build'], vcpkg, timeout=2400)
    name = f'cef-152.0.6-{triplet}-static-wrapper-shared-runtime'
    export = work / 'export'
    run([binary, 'export', spec, '--classic', '--raw', f'--output={name}', f'--output-dir={export}'], vcpkg)
    sdk = export / name
    shutil.copy2(ROOT / 'vcpkg/cef-source.lock.json', sdk / 'cef-source.lock.json')
    shutil.copy2(ROOT / 'vcpkg/ports/cef/usage', sdk / 'CEF-LINKAGE.txt')
    # Move the exported SDK and make the original installation unavailable.
    relocated = work / 'relocated-sdk'
    shutil.move(str(sdk), relocated)
    installed = vcpkg / 'installed'
    installed.rename(vcpkg / 'installed-not-for-tests')
    prefix = relocated / 'installed' / triplet
    if not prefix.is_dir():
        prefix = relocated / triplet
    assert (prefix / 'share/cef/cef-config.cmake').is_file(), prefix
    build = work / 'consumer'
    configure = ['cmake', '-S', ROOT / 'vcpkg/smoke', '-B', build,
                 f'-DCMAKE_PREFIX_PATH={prefix}', '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF']
    if WINDOWS:
        configure += ['-G', 'Visual Studio 17 2022', '-A', 'x64',
                      '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded$<$<CONFIG:Debug>:Debug>']
    else:
        configure += ['-G', 'Ninja Multi-Config']
    run(configure)
    evidence = []
    for config in ['Release', 'Debug']:
        run(['cmake', '--build', build, '--config', config, '--parallel', '3'])
        executable = build / config / ('cef_smoke.exe' if WINDOWS else 'cef_smoke')
        # Test a second relocation: application deploy directory, without the SDK.
        deploy = work / f'deployed-{config}'
        shutil.copytree(executable.parent, deploy)
        hidden = relocated.with_name('sdk-hidden-during-runtime-test')
        relocated.rename(hidden)
        try:
            executable = deploy / executable.name
            if not WINDOWS:
                dependencies = run(['ldd', executable])
                assert 'libcef.so' in dependencies and 'not found' not in dependencies
                dynamic = run(['readelf', '-d', executable])
                assert '$ORIGIN' in dynamic
                (out / f'linkage-{config}.txt').write_text(dependencies + dynamic)
            evidence.append(smoke(executable, out, config))
        finally:
            hidden.rename(relocated)
    receipt = {
        'schema': 1, 'tested_at_epoch': int(time.time()),
        'source_commit': os.environ.get('GITHUB_SHA', run(['git', 'rev-parse', 'HEAD'], ROOT).strip()),
        'cef_commit': LOCK['cef_commit'], 'vcpkg_commit': LOCK['vcpkg_commit'],
        'triplet': triplet, 'wrapper_linkage': 'static', 'engine_linkage': 'shared',
        'full_static_engine_verified': False, 'sandbox_verified': False,
        'sdk_relocation_verified': True, 'application_relocation_verified': True,
        'configurations': evidence
    }
    (relocated / 'build-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    archive = out / f'{name}.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(relocated.rglob('*')):
            if path.is_file():
                bundle.write(path, Path(name) / path.relative_to(relocated))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / f'{archive.name}.sha256').write_text(f'{digest}  {archive.name}\n')
    (out / 'build-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print('HYBRID_BASELINE_VERIFIED; FULL_STATIC_ENGINE_NOT_IMPLEMENTED', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'BUILD FAILED: {error}', file=sys.stderr, flush=True)
        raise
