#!/usr/bin/env python3
"""Export the actual static link edge into a relocatable, C-ABI-only SDK.

No shared runtime download and no guessed list of Chromium libraries. The
reference executable must have passed the source-build runtime test first.
A second, external CMake link/run is required before a release can be certified.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Callable

CEF_COMMIT = '708dc140cbc3286826a8abef89dc23a44ff9ea72'
CHROMIUM_COMMIT = '79460ebecaa5625e57a5fb679a735659e73dc687'
CEF_VERSION = '152.0.6+g708dc14+chromium-152.0.7977.83'
BINARY_SUFFIXES = {'.o', '.obj', '.a', '.lib', '.rlib', '.res'}
FORBIDDEN = re.compile(r'(libcef\.(dll|so)|chrome_elf\.dll|lib(?:egl|glesv2|vk_swiftshader)\.(dll|so)|dxcompiler\.(dll|lib)|dxil\.(dll|lib))', re.I)
# Rust can pass OS import libraries via ldflags rather than GN's libs array.
# Accept only names observed in the pinned Windows graph, not arbitrary paths.
WINDOWS_FLAG_LIBRARIES = frozenset({
    'advapi32.lib', 'bcrypt.lib', 'kernel32.lib', 'ntdll.lib',
    'synchronization.lib', 'userenv.lib', 'ws2_32.lib',
    'legacy_stdio_definitions.lib',
})


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(args: list[str | Path], cwd: Path, *, stdin: str | None = None) -> str:
    command = [str(a) for a in args]
    result = subprocess.run(command, cwd=cwd, input=stdin, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                            errors='replace', timeout=1800)
    if result.returncode:
        raise RuntimeError(f'Export command failed: {command}\n{result.stdout[-20000:]}')
    return result.stdout


def query_link_inputs(query: str) -> list[str]:
    """Parse only direct/implicit inputs of one Ninja link edge, not generators."""
    result: list[str] = []
    in_inputs = False
    for line in query.splitlines():
        if line.startswith('  input:'):
            in_inputs = True
            continue
        if line.startswith('  outputs:'):
            in_inputs = False
        if not in_inputs or not line.startswith('    '):
            continue
        value = line.strip()
        if value.startswith('||'):
            continue  # Order-only action/stamp dependencies are not link inputs.
        value = value.removeprefix('| ').strip()
        if FORBIDDEN.search(value):
            raise RuntimeError(f'Shared engine in native link edge: {value}')
        if re.search(r'\.so(?:\.|$)|\.dll(?:\.|$)|\.dylib$', value, re.I):
            raise RuntimeError(f'Shared library in native link edge: {value}')
        if Path(value).suffix.lower() in BINARY_SUFFIXES:
            result.append(value)
    if not result:
        raise RuntimeError('No object/archive inputs found in native Ninja link edge')
    return list(dict.fromkeys(result))


def smoke_object(path: Path) -> bool:
    return path.suffix.lower() in ('.o', '.obj') and \
        'cef_static_smoke' in path.as_posix() and \
        (path.stem == 'smoke' or path.stem.endswith('.smoke'))


def quote_cmake(value: str) -> str:
    if any(c in value for c in (';', '\n', '\r', '\x00')):
        raise RuntimeError(f'Unsafe CMake argument: {value!r}')
    return '"' + value.replace('\\', '/').replace('"', '\\"') + '"'


def link_options(flags: list[str], windows: bool,
                 stage_link_file: Callable[[str], str]) -> tuple[list[str], list[dict[str, str]]]:
    """Keep semantic/hardening flags; disclose omitted build-host optimizations.

    Unknown options fail rather than being silently erased. Linker driver and
    sysroot selection belong to the consumer, not to a source workspace path.
    """
    kept, omitted = [], []
    for flag in flags:
        if FORBIDDEN.search(flag):
            raise RuntimeError(f'Shared engine flag cannot be exported: {flag}')
        lower = flag.lower()
        reason = None
        if windows:
            if lower in WINDOWS_FLAG_LIBRARIES:
                kept.append(flag)
                continue
            if lower.startswith(('/stack:', '/include:', '/defaultlib:', '/nodefaultlib:',
                                 '/delayload:', '/guard:', '/alternatename:', '/export:')) or lower in (
                    '/dynamicbase', '/nxcompat', '/highentropyva', '/largeaddressaware',
                    '/cetcompat', '/opt:ref', '/opt:icf', '/incremental:no', '/wx', '/fixed:no'):
                kept.append(flag)
                continue
            if lower.startswith(('/machine:', '/subsystem:', '/entry:')):
                # A static engine does not own the consumer's entry point/subsystem.
                # Only the native x64 machine constraint is enforced by the package.
                if lower.startswith('/entry:'):
                    raise RuntimeError(f'Custom engine entry point requires review: {flag}')
                reason = 'consumer owns executable subsystem; package checks x64'
            elif lower.startswith(('/timestamp:', '/pdb', '/debug', '/natvis:',
                                   '/manifest', '/profile', '/filealign:', '/opt:',
                                   '/lld', '/prefetch-', '/mllvm', '/call-graph-profile-sort',
                                   '/ignore:')):
                reason = 'build-host diagnostics, layout or compiler-specific optimization'
            elif lower in ('/nologo', '/brepro', '/fastfail', '--color-diagnostics'):
                reason = 'build-host linker control'
        else:
            if flag in ('-m64', '-pthread', '-pie', '-rdynamic', '-Werror',
                        '-Wl,-z,defs', '-Wl,-z,noexecstack', '-Wl,-z,relro', '-Wl,-z,now',
                        '-Wl,--as-needed', '-Wl,--gc-sections', '-Wl,--no-undefined',
                        '-Wl,--fatal-warnings', '-Wl,--export-dynamic',
                        '-Wl,--disable-new-dtags', '-Wl,-rpath=$ORIGIN') or flag.startswith((
                        '-Wl,--export-dynamic-symbol=', '-Wl,--wrap=', '-Wl,-u,',
                        '-Wl,--undefined=', '-Wl,-z,max-page-size=')):
                kept.append(flag)
                continue
            matched = False
            for prefix in ('-Wl,--version-script=', '-Wl,--dynamic-list='):
                if flag.startswith(prefix):
                    kept.append(prefix + stage_link_file(flag[len(prefix):]))
                    matched = True
                    break
            if matched:
                continue
            if flag.startswith(('--sysroot=', '--ld-path=', '-B', '-L')) or flag in (
                    '-fuse-ld=lld', '-nostdlib++', '--unwindlib=none', '-no-canonical-prefixes',
                    '--target=x86_64-unknown-linux-gnu'):
                reason = 'consumer compiler/sysroot selection; C API needs no C++ compile ABI'
            elif flag.startswith(('-Wl,--build-id', '-Wl,--icf=', '-Wl,--color-diagnostics',
                                  '-Wl,--no-call-graph-profile-sort', '-Wl,--read-workers=',
                                  '-Wl,--strict-auto-link', '-Wl,--pack-dyn-relocs=',
                                  '-Wl,--undefined-version', '-Wl,--no-rosegment',
                                  '-Wl,--lto-O', '-Wl,--thinlto-', '-Wl,-O', '-fuse-ld=')):
                reason = 'build-host optimization or linker-version-specific control'
            elif flag in ('-Wl,-z,keep-text-section-prefix', '-fPIC', '-g0'):
                reason = 'build-host layout/diagnostic choice'
        if reason is None:
            raise RuntimeError(f'Unreviewed native link option: {flag}')
        omitted.append({'flag': flag, 'reason': reason})
    return list(dict.fromkeys(kept)), omitted


def verify_reference(receipt: dict) -> None:
    if receipt.get('cef_commit') != CEF_COMMIT or receipt.get('chromium_commit') != CHROMIUM_COMMIT:
        raise RuntimeError('Reference receipt source pin mismatch')
    if receipt.get('source_build_verified') is not True or receipt.get('engine_linkage') != 'static':
        raise RuntimeError('SDK export requires a real successful static engine build/run')
    proof = receipt.get('smoke', {})
    if proof.get('cef') != CEF_VERSION or proof.get('engine') != 'static':
        raise RuntimeError('Reference receipt engine/version mismatch')
    for field in ('javascript', 'paint', 'browser_modules_clean', 'renderer_modules_clean'):
        if proof.get(field) is not True:
            raise RuntimeError(f'Reference runtime check was not verified: {field}')
    if type(proof.get('renderer_pid')) is not int or proof['renderer_pid'] <= 0 or \
            type(proof.get('browser_pid')) is not int or proof['browser_pid'] <= 0 or \
            proof['renderer_pid'] == proof['browser_pid']:
        raise RuntimeError('Reference receipt has no separate renderer proof')


def export(source: Path, out: Path, diagnostics: Path, prefix: Path) -> None:
    windows = os.name == 'nt'
    receipt = json.loads((diagnostics/'engine-build-receipt.json').read_text())
    verify_reference(receipt)
    if prefix.exists() and any(prefix.iterdir()):
        raise RuntimeError('SDK destination must be empty')
    prefix.mkdir(parents=True, exist_ok=True)
    share = prefix/'share/cef-static'; share.mkdir(parents=True)
    lib = prefix/'lib/cef-static'; lib.mkdir(parents=True)
    tools = source/'third_party/llvm-build/Release+Asserts/bin'
    ninja = source/'third_party/ninja'/('ninja.exe' if windows else 'ninja')
    exe_name = 'cef_static_smoke.exe' if windows else 'cef_static_smoke'
    native_exe = out/exe_name
    if sha256(native_exe) != receipt.get('executable_sha256'):
        raise RuntimeError('Native executable was changed after runtime verification')
    query = run([ninja, '-C', out, '-t', 'query', exe_name], source)
    (share/'native-link-edge.txt').write_text(query)
    files = query_link_inputs(query)
    graph = json.loads((diagnostics/'gn-graph.json').read_text())
    root = graph['//cef:cef_static_smoke']
    objects, archives, resources, system_libs, manifest = [], [], [], [], []
    excluded_smoke = []
    seen: set[Path] = set()
    def add_input(value: str) -> None:
        path = (source/value[2:] if value.startswith('//') else out/value).resolve()
        if path in seen:
            return
        if not path.is_file() or not path.is_relative_to(source):
            raise RuntimeError(f'Unresolved or external native link input: {value}')
        seen.add(path)
        if smoke_object(path):
            excluded_smoke.append(path)
        elif path.suffix.lower() in ('.o', '.obj'):
            objects.append(path)
        elif path.suffix.lower() == '.res':
            resources.append(path)
        elif path.suffix.lower() in ('.a', '.lib', '.rlib'):
            archives.append(path)
        else:
            raise RuntimeError(f'Unsupported link input: {path}')
    for value in files:
        add_input(value)
    for value in root.get('libs', []):
        if '/' in value or '\\' in value:
            add_input(value)
        elif FORBIDDEN.search(value):
            raise RuntimeError(f'Forbidden system library name: {value}')
        else:
            system_libs.append(value)
    if len(excluded_smoke) != 1 or not archives:
        raise RuntimeError('Cannot isolate exactly one reference app object and a nonempty engine closure')
    for path in objects + archives + resources:
        manifest.append({'source': path.relative_to(source).as_posix(), 'sha256': sha256(path)})
    suffix = '.lib' if windows else '.a'
    ar = tools/('lld-link.exe' if windows else 'llvm-ar')
    def response(paths: list[Path], file: Path) -> None:
        file.write_text('\n'.join('"'+p.as_posix()+'"' for p in paths)+'\n', encoding='utf-8')
    cmake = ['# Generated from the native GN/Ninja link edge. Do not hand-edit.',
             'if(CMAKE_VERSION VERSION_LESS 3.24)',
             '  message(FATAL_ERROR "cef-static requires CMake 3.24 or newer")',
             'endif()',
             'get_filename_component(_cef_static_prefix "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)',
             'if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)',
             '  message(FATAL_ERROR "cef-static supports only native x64 consumers")',
             'endif()',
             'if(TARGET CEF::static)', '  return()', 'endif()',
             'add_library(CEF::static INTERFACE IMPORTED GLOBAL)',
             'set_property(TARGET CEF::static PROPERTY INTERFACE_INCLUDE_DIRECTORIES "${_cef_static_prefix}/include/cef-static")',
             'set_property(TARGET CEF::static PROPERTY INTERFACE_COMPILE_DEFINITIONS "CEF_STATIC;CEF_API_VERSION=15200")']
    dependencies = []
    if objects:
        archive = lib/('cef_objects'+suffix)
        rsp = diagnostics/'static-objects.rsp'; response(objects,rsp)
        if windows:
            run([ar, '/lib', '/nologo', '/OUT:'+str(archive), '@'+str(rsp)], out)
        else:
            run([ar, 'qcsD', archive, '@'+str(rsp)], out)
        dependencies.append('$<LINK_LIBRARY:WHOLE_ARCHIVE,CEF::objects>')
        cmake += ['add_library(CEF::objects STATIC IMPORTED GLOBAL)',
                  'set_property(TARGET CEF::objects PROPERTY IMPORTED_LOCATION "${_cef_static_prefix}/lib/cef-static/'+archive.name+'")']
    library_targets = []
    for index, path in enumerate(archives):
        name = f'cef_{index:04d}_'+hashlib.sha256(path.relative_to(source).as_posix().encode()).hexdigest()[:12]+suffix
        dest = lib/name
        with path.open('rb') as f:
            magic = f.read(8)
        if magic == b'!<thin>\n':
            if windows:
                run([ar, '/lib', '/nologo', '/OUT:'+str(dest), path], out)
            else:
                run([ar, '-M'], out, stdin=f'CREATE "{dest.as_posix()}"\nADDLIB "{path.as_posix()}"\nSAVE\nEND\n')
        elif magic == b'!<arch>\n':
            shutil.copy2(path,dest)
        else:
            raise RuntimeError(f'Not an archive: {path}')
        with dest.open('rb') as f:
            if f.read(8) != b'!<arch>\n':
                raise RuntimeError(f'Non-relocatable/thin output archive: {dest}')
        target = f'CEF::archive_{index}'
        library_targets.append(target)
        cmake += [f'add_library({target} STATIC IMPORTED GLOBAL)',
                  f'set_property(TARGET {target} PROPERTY IMPORTED_LOCATION "${{_cef_static_prefix}}/lib/cef-static/{name}")']
    if windows:
        dependencies += library_targets
    else:
        dependencies.append('$<LINK_GROUP:RESCAN,'+','.join(library_targets)+'>')
    for index, path in enumerate(resources):
        dest = lib/f'cef_resource_{index}.res'; shutil.copy2(path,dest)
        dependencies.append('${_cef_static_prefix}/lib/cef-static/'+dest.name)
    dependencies += system_libs
    def stage_link_file(value: str) -> str:
        path = (out/value).resolve()
        if not path.is_file() or not path.is_relative_to(source):
            raise RuntimeError('Link script is outside pinned source: '+value)
        dest = share/'linker'/(hashlib.sha256(value.encode()).hexdigest()[:12]+'-'+path.name)
        dest.parent.mkdir(exist_ok=True); shutil.copy2(path,dest)
        return '${_cef_static_prefix}/share/cef-static/linker/'+dest.name
    flags, omitted = link_options(root.get('ldflags', []), windows, stage_link_file)
    cmake += ['set_property(TARGET CEF::static PROPERTY INTERFACE_LINK_LIBRARIES',
              *['  '+quote_cmake(x) for x in dependencies], ')',
              'set_property(TARGET CEF::static PROPERTY INTERFACE_LINK_OPTIONS',
              *['  '+quote_cmake(x) for x in flags], ')',
              'set(CEF_STATIC_VERSION "'+CEF_VERSION+'")',
              'set(CEF_STATIC_CAPI_ONLY TRUE)',
              'set(CEF_STATIC_ENGINE_CONFIGURATION Release)',
              'set(CEF_STATIC_RESOURCES "${_cef_static_prefix}/share/cef-static/resources")',
              'function(cef_static_deploy_resources target)',
              '  if(NOT TARGET "${target}")',
              '    message(FATAL_ERROR "cef_static_deploy_resources: unknown target")',
              '  endif()',
              '  add_custom_command(TARGET "${target}" POST_BUILD COMMAND "${CMAKE_COMMAND}" -E copy_directory "${CEF_STATIC_RESOURCES}" "$<TARGET_FILE_DIR:${target}>" VERBATIM)',
              'endfunction()', '']
    (share/'cef-static-config.cmake').write_text('\n'.join(cmake),encoding='utf-8')
    (share/'cef-static-config-version.cmake').write_text('set(PACKAGE_VERSION "152.0.6")\nif(PACKAGE_FIND_VERSION VERSION_GREATER PACKAGE_VERSION)\n set(PACKAGE_VERSION_COMPATIBLE FALSE)\nelse()\n set(PACKAGE_VERSION_COMPATIBLE TRUE)\n if(PACKAGE_FIND_VERSION VERSION_EQUAL PACKAGE_VERSION)\n  set(PACKAGE_VERSION_EXACT TRUE)\n endif()\nendif()\n')
    include = prefix/'include/cef-static/include'
    shutil.copytree(source/'cef/include', include)
    for path in (out/'gen/cef/include').rglob('*.h'):
        dest = include/path.relative_to(out/'gen/cef/include')
        dest.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(path,dest)
    # Same header transfer required by the upstream binary distributor.
    shutil.copy2(source/'net/base/net_error_list.h', include/'base/internal/cef_net_error_list.h')
    for required in ('cef_version.h','cef_config.h','cef_api_versions.h','capi/cef_app_capi.h'):
        if not (include/required).is_file():
            raise RuntimeError('Missing generated public header: '+required)
    resource_dir = share/'resources'; resource_dir.mkdir()
    for name in ('icudtl.dat','resources.pak','chrome_100_percent.pak','chrome_200_percent.pak',
                 'snapshot_blob.bin','v8_context_snapshot.bin'):
        if (out/name).is_file(): shutil.copy2(out/name,resource_dir/name)
    if (out/'locales').is_dir(): shutil.copytree(out/'locales',resource_dir/'locales')
    licenses = share/'licenses'; licenses.mkdir()
    shutil.copy2(source/'cef/LICENSE.txt', share/'copyright')
    shutil.copy2(source/'LICENSE',licenses/'Chromium-LICENSE')
    # Include legal texts without distributing the compiler, fonts or OS SDKs.
    for directory in ('cef','third_party','v8/third_party'):
        for path in (source/directory).rglob('*'):
            if path.is_file() and (path.name in ('LICENSE','LICENSE.txt','LICENSE.chromium','COPYING','COPYRIGHT','README.chromium') or path.name.startswith('LICENSE.')):
                if '.git' in path.parts or path.stat().st_size > 8*1024*1024:
                    continue
                relative = path.relative_to(source)
                dest = licenses/relative; dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(path,dest)
    (share/'reference-build-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    inventory = {'schema':1,'cef_commit':CEF_COMMIT,'chromium_commit':CHROMIUM_COMMIT,
                 'objects':len(objects),'archives':len(archives),'resources':len(resources),
                 'system_libraries':system_libs,'native_inputs':manifest,
                 'semantic_link_flags':flags,'omitted_build_host_flags':omitted,
                 'engine_linkage':'static','capi_only':True,
                 'sdk_external_consumer_verified':False,
                 'package_files':[{'path':p.relative_to(prefix).as_posix(),'sha256':sha256(p)}
                                  for p in sorted(prefix.rglob('*')) if p.is_file() and
                                  p.is_relative_to(lib)]}
    (share/'static-link-inventory.json').write_text(json.dumps(inventory,indent=2)+'\n')
    print(f'Exported static link closure: {len(objects)} forced objects, {len(archives)} static archives')
    print('External SDK consumer link/run is still REQUIRED before publication')


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--diagnostics',type=Path,required=True)
    p.add_argument('--prefix',type=Path,required=True)
    a=p.parse_args()
    export(a.source.resolve(),a.out.resolve(),a.diagnostics.resolve(),a.prefix.resolve())

if __name__=='__main__':
    main()
