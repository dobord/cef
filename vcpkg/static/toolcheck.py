#!/usr/bin/env python3
"""Run launcher checks in real vcpkg scope before the expensive source checkout."""
import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import sys
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('source_build', ROOT/'vcpkg/ports/cef-static/source_build.py')
build=importlib.util.module_from_spec(spec); spec.loader.exec_module(build)
PIN='3723ec118c8354290925feb58d021a9205a3e772'
def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--triplet',required=True,
        choices=['x64-linux','x64-windows-static']); args=parser.parse_args()
    manager=Path(os.environ['RUNNER_TEMP'])/'cef-vcpkg-manager'
    logs=ROOT/'static-diagnostics/toolcheck'; logs.mkdir(parents=True,exist_ok=True)
    os.environ['VCPKG_BINARY_SOURCES']='clear'
    run=lambda command,cwd,name: build.run(command,cwd,logs,name,timeout=1200)
    try:
        run(['git','init',manager],ROOT,'init')
        run(['git','fetch','--depth=1','https://github.com/microsoft/vcpkg.git',PIN],manager,'fetch')
        run(['git','checkout','--detach','FETCH_HEAD'],manager,'checkout')
        run(['cmd','/c','bootstrap-vcpkg.bat','-disableMetrics'] if os.name=='nt' else
            ['bash','bootstrap-vcpkg.sh','-disableMetrics'],manager,'bootstrap')
        binary=manager/('vcpkg.exe' if os.name=='nt' else 'vcpkg')
        run([binary,'install','cef-static-toolcheck:'+args.triplet,'--classic',
            '--overlay-ports='+str(ROOT/'vcpkg/static/toolcheck-port')],manager,'install')
        receipt=manager/'installed'/args.triplet/'share/cef-static-toolcheck/toolchain.json'
        shutil.copy2(receipt,logs/'toolchain.json')
        print('VCPKG_NATIVE_LAUNCHERS_VERIFIED; NO_ENGINE_BUILD_CLAIM',flush=True)
    finally:
        tree=manager/'buildtrees/cef-static-toolcheck'
        if tree.exists():
            for path in tree.rglob('*'):
                if path.is_file() and path.suffix in ('.log','.json'):
                    dest=logs/path.relative_to(tree); dest.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(path,dest)
if __name__=='__main__': main()
