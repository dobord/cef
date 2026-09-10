#!/usr/bin/env python3
"""Native source-build vcpkg CI. Publication requires external SDK link/run."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PORT = ROOT/'vcpkg/ports/cef-static'
VCPKG_COMMIT = '3723ec118c8354290925feb58d021a9205a3e772'
WINDOWS = os.name == 'nt'
spec = importlib.util.spec_from_file_location('cef_source_build',PORT/'source_build.py')
assert spec and spec.loader
build = importlib.util.module_from_spec(spec); spec.loader.exec_module(build)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--triplet', required=True, choices=['x64-linux','x64-windows-static'])
    args = parser.parse_args()
    if WINDOWS != (args.triplet == 'x64-windows-static'):
        raise RuntimeError('Native triplet required')
    work = Path(os.environ['RUNNER_TEMP'])/'cef-static'
    os.environ['CEF_STATIC_WORK'] = str(work)
    os.environ['VCPKG_BINARY_SOURCES'] = 'clear'
    os.environ['VCPKG_MAX_CONCURRENCY'] = '4'
    artifacts = ROOT/'static-artifacts'; artifacts.mkdir(exist_ok=True)
    diagnostics = ROOT/'static-diagnostics'; diagnostics.mkdir(exist_ok=True)
    manager = Path(os.environ['RUNNER_TEMP'])/'cef-vcpkg-manager'
    run = lambda command, cwd, name: build.run(command,cwd,diagnostics,name,timeout=21600)
    try:
        run(['git','init',manager],ROOT,'vcpkg-init')
        run(['git','fetch','--depth=1','https://github.com/microsoft/vcpkg.git',VCPKG_COMMIT],manager,'vcpkg-fetch')
        run(['git','checkout','--detach','FETCH_HEAD'],manager,'vcpkg-checkout')
        run(['cmd','/c','bootstrap-vcpkg.bat','-disableMetrics'] if WINDOWS else
            ['bash','bootstrap-vcpkg.sh','-disableMetrics'],manager,'vcpkg-bootstrap')
        binary = manager/('vcpkg.exe' if WINDOWS else 'vcpkg')
        run([binary,'install',f'cef-static:{args.triplet}','--classic',
             f'--overlay-ports={ROOT / "vcpkg/ports"}',
             f'--overlay-triplets={ROOT / "vcpkg/static/triplets"}'],manager,'vcpkg-install')
        export = work/'vcpkg-export'
        name = f'cef-152.0.6-{args.triplet}-static-engine-capi'
        run([binary,'export',f'cef-static:{args.triplet}','--classic','--raw',
             f'--output={name}',f'--output-dir={export}'],manager,'vcpkg-export')
        sdk = work/'relocated-static-sdk'
        shutil.move(str(export/name),sdk)
        installed = manager/'installed'
        installed.rename(manager/'installed-hidden-for-consumer-test')
        prefix = sdk/'installed'/args.triplet
        if not (prefix/'share/cef-static/cef-static-config.cmake').is_file():
            raise RuntimeError('Missing exported CMake package')
        consumer = work/'external-static-consumer'
        configure = ['cmake','-S',ROOT/'vcpkg/static/consumer','-B',consumer,
                     f'-DCMAKE_PREFIX_PATH={prefix}',
                     f'-DCEF_STATIC_SMOKE_SOURCE={PORT / "smoke.c"}',
                     '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF',
                     '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF']
        configure += ['-G','Visual Studio 17 2022','-A','x64'] if WINDOWS else ['-G','Unix Makefiles','-DCMAKE_BUILD_TYPE=Release']
        run(configure,ROOT,'external-consumer-configure')
        run(['cmake','--build',consumer,'--config','Release','--parallel','4'],ROOT,'external-consumer-build')
        binary_dir = consumer/'Release' if WINDOWS else consumer
        deployed = work/'relocated-static-application'
        shutil.copytree(binary_dir,deployed,ignore=shutil.ignore_patterns('CMakeFiles','CMakeCache.txt','*.vcxproj*','*.sln','Makefile'))
        executable = deployed/('cef_static_smoke.exe' if WINDOWS else 'cef_static_smoke')
        # Hide both SDK and the entire Chromium tree during execution. No
        # source-workspace runtime fallback can make this test accidentally pass.
        original_source = work/'download'
        hidden_source = work/'source-hidden-for-runtime-test'
        hidden_sdk = work/'sdk-hidden-for-runtime-test'
        sdk.rename(hidden_sdk); original_source.rename(hidden_source)
        try:
            proof = build.execute_smoke(executable,diagnostics)
            if not WINDOWS:
                dependency_log = run(['ldd',executable],ROOT,'external-runtime-libraries')
                if 'not found' in dependency_log or 'libcef.so' in dependency_log:
                    raise RuntimeError('External app has unresolved/shared-engine dependencies')
                run(['readelf','-d',executable],ROOT,'external-binary-imports')
        finally:
            hidden_source.rename(original_source); hidden_sdk.rename(sdk)
        receipt = {'schema':1,'integration_commit':os.environ['GITHUB_SHA'],
                   'vcpkg_commit':VCPKG_COMMIT,'cef_commit':build.CEF,
                   'chromium_commit':build.CHROMIUM,'triplet':args.triplet,
                   'configuration':'Release','engine_linkage':'static',
                   'capi_only':True,'sdk_relocation_verified':True,
                   'application_relocation_verified':True,'sandbox_verified':False,
                   'system_libraries_static':False,'smoke':proof,
                   'executable_sha256':build.digest(executable)}
        (sdk/'static-sdk-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        (artifacts/f'{name}.json').write_text(json.dumps(receipt,indent=2)+'\n')
        archive = artifacts/f'{name}.zip'
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
            for path in sorted(sdk.rglob('*')):
                if path.is_file(): bundle.write(path,Path(name)/path.relative_to(sdk))
        if archive.stat().st_size >= 2*1024**3:
            raise RuntimeError('SDK ZIP exceeds the per-asset release size limit; do not publish it unsplit')
        (artifacts/f'{archive.name}.sha256').write_text(build.digest(archive)+'  '+archive.name+'\n')
        print('STATIC_ENGINE_VCPKG_EXTERNAL_CAPI_CONSUMER_VERIFIED',flush=True)
    finally:
        port_logs = manager/'buildtrees/cef-static'
        if port_logs.is_dir():
            candidates = list((port_logs/'diagnostics').rglob('*')) + list(port_logs.glob('*.log'))
            for path in candidates:
                if path.is_file() and (path.suffix in ('.log','.json') or path.name=='args.gn'):
                    dest = diagnostics/'port'/path.relative_to(port_logs)
                    dest.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(path,dest)

if __name__=='__main__': main()
