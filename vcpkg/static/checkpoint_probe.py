#!/usr/bin/env python3
"""Two-runner native archive fixture. This does not build or certify CEF."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import checkpoint as cp


def native_ninja():
    # Chocolatey may put a console-launcher shim before the native Ninja.
    # CTRL_BREAK must target Ninja itself, as the CEF build does with its pin.
    if os.name == 'nt':
        vswhere = Path(os.environ['ProgramFiles(x86)'])/'Microsoft Visual Studio/Installer/vswhere.exe'
        install = subprocess.check_output([str(vswhere), '-latest', '-products', '*',
            '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
            '-property', 'installationPath'], text=True).strip()
        ninja = Path(install)/'Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe'
        if not ninja.is_file():
            raise RuntimeError('Native Visual Studio Ninja is required for the Windows regression')
        return str(ninja)
    return shutil.which('ninja')


def run(args, cwd=None):
    print('+', subprocess.list2cmdline(list(map(str, args))), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, check=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['produce','consume']);args=parser.parse_args()
    work=Path(os.environ['RUNNER_TEMP'])/'cef-checkpoint-probe'
    package=Path('checkpoint-probe-artifact').resolve()
    identity={'recipe':'native-archive-fixture-v2-links','work':str(work.resolve()),
              'image':os.environ.get('ImageVersion'), 'os':os.name}
    if args.phase=='produce':
        work.mkdir()
        (work/'value.h').write_text('#define RESULT 42\nint value(void);\n')
        os.symlink('value.h', work/'value-link.h')
        (work/'depot_tools').mkdir()
        (work/'depot_tools/cros').write_text('native fixture, not depot_tools\n')
        os.symlink('cros', work/'depot_tools/cros_sdk')
        secret = work/next(iter(cp.OMITTED_FILES))
        secret.parent.mkdir(parents=True)
        secret.write_text('SYNTHETIC-CREDENTIAL-NOT-FOR-ARCHIVE\n')
        (work/'first.c').write_text('#include "value-link.h"\nint value(void) { return RESULT; }\n')
        (work/'main.c').write_text('#include <stdio.h>\n#include "value.h"\nint main(void){ printf("%d\\n",value());return 0;}\n')
        (work/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.24)\n'
            'project(checkpoint_probe C)\nadd_library(first STATIC first.c)\n'
            'add_executable(probe main.c)\ntarget_link_libraries(probe PRIVATE first)\n')
        run(['cmake','-S',work,'-B',work/'build','-G','Ninja','-DCMAKE_BUILD_TYPE=Release','-DCMAKE_MAKE_PROGRAM='+native_ninja()])
        run(['cmake','--build',work/'build','--target','first'])
        obj=next((work/'build/CMakeFiles/first.dir').glob('*.obj' if os.name=='nt' else '*.o'))
        descriptor={'object':obj.relative_to(work).as_posix(),'mtime_ns':obj.stat().st_mtime_ns,'sha256':cp.digest(obj)}
        (work/'fixture.json').write_text(json.dumps(descriptor))
        result=cp.save(work,package,identity,limit=32768)
        print(json.dumps({'fixture_checkpoint_files':result['files'],'engine_verified':False}),flush=True)
    else:
        cp.restore(package,work,identity)
        if not (work/'value-link.h').is_symlink() or not (work/'depot_tools/cros_sdk').is_symlink():
            raise RuntimeError('Internal symlinks lost during checkpoint handoff')
        if (work/next(iter(cp.OMITTED_FILES))).exists():
            raise RuntimeError('Telemetry credentials leaked into checkpoint')
        descriptor=json.loads((work/'fixture.json').read_text());obj=work/descriptor['object']
        if obj.stat().st_mtime_ns!=descriptor['mtime_ns'] or cp.digest(obj)!=descriptor['sha256']:
            raise RuntimeError('Object changed during handoff')
        run(['cmake','--build',work/'build','--target','probe'])
        exe=work/'build'/('probe.exe' if os.name=='nt' else 'probe')
        actual=subprocess.check_output([str(exe)],text=True).strip()
        if actual!='42' or obj.stat().st_mtime_ns!=descriptor['mtime_ns']:
            raise RuntimeError('Ninja did not reuse the completed object on the fresh runner')
        # The deps log must still invalidate a previously compiled object.
        (work/'value.h').write_text('#define RESULT 43\nint value(void);\n')
        run(['cmake','--build',work/'build','--target','probe'])
        if subprocess.check_output([str(exe)],text=True).strip()!='43' or cp.digest(obj)==descriptor['sha256']:
            raise RuntimeError('Restored dependency tracking did not rebuild the changed header')
        evidence={'native_fixture':True,'fresh_runner_object_reuse':True,
                  'changed_header_recompiled':True,'internal_symlinks_restored':True,
                  'telemetry_credentials_omitted':True,'engine_build_verified':False}
        Path('checkpoint-probe-result.json').write_text(json.dumps(evidence,indent=2)+'\n')
        print('NATIVE_CHECKPOINT_HANDOFF_VERIFIED; NOT_A_CEF_BUILD',flush=True)

if __name__=='__main__':main()
