#!/usr/bin/env python3
"""Two-runner native archive fixture. This does not build or certify CEF."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import checkpoint as cp
import runner_fingerprint
import runner_image_migration


# This scope belongs only to the hard-coded CMake/Ninja C fixture below.
# Real CEF checkpoint/image migration continues to use the full host inventory.
NINJA_C_PROFILE = 'native-ninja-c-v1'
NINJA_C_REQUIRED_COMPONENTS = frozenset({
    'cmake-bin', 'cmake-share', 'git-bin', 'git-cmd', 'git-core',
    'msvc', 'vc-build', 'vc-include', 'ninja', 'vswhere',
    'sdk-bin', 'sdk-include', 'sdk-lib', 'netfx-sdk',
    'python-exe', 'python-dlls', 'python-stdlib',
})


def fixture_toolchain_digest(host: dict) -> str:
    """Exclude only unused MSBuild from this native C/Ninja fixture's identity.

    Keep the full, validated inventory in the host evidence. CMake is explicitly
    invoked with -G Ninja, and the fixture has no MSBuild/custom build steps.
    Hash every other component (including unknown future ones) conservatively.
    This is not an admission rule for Chromium checkpoints.
    """
    runner_image_migration.checked_digest(host)
    components = host['components']
    missing = NINJA_C_REQUIRED_COMPONENTS - components.keys()
    if missing:
        raise ValueError('Incomplete native C/Ninja inventory: ' + ', '.join(sorted(missing)))
    if not any(name.startswith('python-python') and name.endswith('.dll')
               for name in components):
        raise ValueError('Missing native Python runtime inventory')
    selected = {name: value for name, value in components.items() if name != 'msbuild'}
    return hashlib.sha256(runner_fingerprint.json_bytes({
        'profile': NINJA_C_PROFILE, 'components': selected})).hexdigest()


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


def fixture_identity(work, host=None):
    identity = {'recipe': 'native-archive-fixture-v4-host-content',
                'work': str(work.resolve()), 'os': os.name}
    if os.name == 'nt':
        identity['recipe'] = 'native-archive-fixture-v5-ninja-c-content'
        identity['host_toolchain_profile'] = NINJA_C_PROFILE
        identity['host_toolchain_sha256'] = fixture_toolchain_digest(
            host if host is not None else runner_fingerprint.collect())
    else:
        # Linux retains its existing image-bound policy.
        identity['image'] = os.environ.get('ImageVersion')
    return identity


def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['produce','consume']);args=parser.parse_args()
    work=Path(os.environ['RUNNER_TEMP'])/'cef-checkpoint-probe'
    package=Path('checkpoint-probe-artifact').resolve()
    host = runner_fingerprint.collect() if os.name == 'nt' else None
    identity = fixture_identity(work, host)
    Path('checkpoint-probe-host.json').write_text(json.dumps({
        'image': os.environ.get('ImageVersion'), 'identity': identity,
        'toolchain': host, 'engine_build_verified': False}, indent=2) + '\n')
    if args.phase=='produce':
        work.mkdir()
        (work/'value.h').write_text('#define RESULT 42\nint value(void);\n')
        # Keep a normal header dependency plus an alias dependency. Older
        # native Windows Ninja stats a symlink rather than following its target.
        # Alias-clock preservation and normal-header invalidation are distinct checks.
        (work/'alias-target.h').write_text('#define CHECKPOINT_BIAS 0\n')
        os.symlink('alias-target.h', work/'value-link.h')
        (work/'depot_tools').mkdir()
        (work/'depot_tools/cros').write_text('native fixture, not depot_tools\n')
        os.symlink('cros', work/'depot_tools/cros_sdk')
        secret = work/next(iter(cp.OMITTED_FILES))
        secret.parent.mkdir(parents=True)
        secret.write_text('SYNTHETIC-CREDENTIAL-NOT-FOR-ARCHIVE\n')
        (work/'first.c').write_text('#include "value.h"\n#include "value-link.h"\nint value(void) { return RESULT + CHECKPOINT_BIAS; }\n')
        (work/'main.c').write_text('#include <stdio.h>\n#include "value.h"\nint main(void){ printf("%d\\n",value());return 0;}\n')
        (work/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.24)\n'
            'project(checkpoint_probe C)\nadd_library(first STATIC first.c)\n'
            'add_executable(probe main.c)\ntarget_link_libraries(probe PRIVATE first)\n')
        run(['cmake','-S',work,'-B',work/'build','-G','Ninja','-DCMAKE_BUILD_TYPE=Release','-DCMAKE_MAKE_PROGRAM='+native_ninja()])
        run(['cmake','--build',work/'build','--target','first'])
        obj=next((work/'build/CMakeFiles/first.dir').glob('*.obj' if os.name=='nt' else '*.o'))
        descriptor={'producer_image':os.environ.get('ImageVersion'),
                    'object':obj.relative_to(work).as_posix(),'mtime_ns':obj.stat().st_mtime_ns,'sha256':cp.digest(obj),
                    'link_mtime_ns':(work/'value-link.h').lstat().st_mtime_ns,
                    'target_mtime_ns':(work/'alias-target.h').stat().st_mtime_ns}
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
        if ((work/'value-link.h').lstat().st_mtime_ns != descriptor['link_mtime_ns'] or
                (work/'alias-target.h').stat().st_mtime_ns != descriptor['target_mtime_ns']):
            raise RuntimeError('Symlink or target header clock changed during handoff')
        run([native_ninja(), '-C', work/'build', '-d', 'explain', '-n', 'probe'])
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
        evidence={'producer_image':descriptor['producer_image'],
                  'consumer_image':os.environ.get('ImageVersion'),
                  'host_toolchain_sha256':identity.get('host_toolchain_sha256'),
                  'native_fixture':True,'fresh_runner_object_reuse':True,
                  'changed_header_recompiled':True,'internal_symlinks_restored':True,
                  'telemetry_credentials_omitted':True,'symlink_timestamps_preserved':True,'engine_build_verified':False}
        Path('checkpoint-probe-result.json').write_text(json.dumps(evidence,indent=2)+'\n')
        print('NATIVE_CHECKPOINT_HANDOFF_VERIFIED; NOT_A_CEF_BUILD',flush=True)

if __name__=='__main__':main()
