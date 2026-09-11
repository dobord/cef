#!/usr/bin/env python3
"""Build the pinned CEF/Chromium engine from source. No binary SDK fallback.

Stages are explicit. A successful prepare/configure is NOT a successful engine
build. Only a linked, relocated executable with a valid runtime proof qualifies.
"""
from __future__ import annotations
from contextlib import contextmanager, ExitStack
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
import time
import urllib.request

HERE = Path(__file__).resolve().parent
CEF = '708dc140cbc3286826a8abef89dc23a44ff9ea72'
CHROMIUM = '79460ebecaa5625e57a5fb679a735659e73dc687'
CHROMIUM_VERSION = '152.0.7977.83'
DEPOT = '08f3e8c0eb66d6de3a048a757d0ff708dbc8ea34'
AUTOMATE_SHA256 = 'fe0c880fd2a91ac3ab4c82301f596295cecc1901e503507e36300a5b58578dcd'
WINDOWS = os.name == 'nt'


def positive_env(name: str, default: int, maximum: int) -> int:
    """Validate operational limits before starting expensive subprocesses."""
    value = os.environ.get(name, str(default))
    try:
        number = int(value)
    except ValueError as error:
        raise ValueError(f'{name} must be an integer, got {value!r}') from error
    if not 1 <= number <= maximum:
        raise ValueError(f'{name} must be between 1 and {maximum}, got {number}')
    return number


def build_timeout() -> int:
    # Hosts may set a larger budget; no timeout ever counts as build success.
    return positive_env('CEF_STATIC_BUILD_TIMEOUT_SECONDS', 18000, 172800)


def digest(path: Path) -> str:
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def kill_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if WINDOWS:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], check=False)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=30)


def run(command: list[str | Path], cwd: Path, logs: Path, name: str,
        timeout: int = 3600, quiet: bool = False) -> str:
    args = list(map(str, command))
    if args and args[0].lower() in ('git', 'git.exe'):
        args[0] = git_program()
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / (name + '.log')
    print(f'[{dt.datetime.now(dt.timezone.utc).isoformat()}] {name}: '
          + subprocess.list2cmdline(args), flush=True)
    with log_path.open('w', encoding='utf-8') as log:
        process = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True,
                                   encoding='utf-8', errors='replace', bufsize=1,
                                   start_new_session=not WINDOWS)
        def drain() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line); log.flush()
                if not quiet:
                    print(line, end='', flush=True)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        started = time.monotonic()
        status = {'schema': 1, 'name': name, 'command': args,
                  'timeout_seconds': timeout, 'status': 'running'}
        try:
            code = process.wait(timeout=timeout)
            status['status'] = 'success' if code == 0 else 'failed'
        except subprocess.TimeoutExpired as error:
            status['status'] = 'timed_out'
            kill_tree(process)
            raise RuntimeError(
                f'{name} timed out after {timeout}s; see {log_path}. '
                'No build success is recorded. The source workspace is preserved '
                'for an incremental retry.') from error
        except BaseException:
            status['status'] = 'interrupted'
            kill_tree(process)
            raise
        finally:
            reader.join(timeout=20)
            # Popen owns the pipe; the reader does not close it automatically.
            if not reader.is_alive() and process.stdout is not None:
                process.stdout.close()
            status['elapsed_seconds'] = round(time.monotonic() - started, 3)
            status['exit_code'] = process.poll()
            (logs / (name + '-status.json')).write_text(
                json.dumps(status, indent=2) + '\n', encoding='utf-8')
    if code != 0:
        raise RuntimeError(f'{name} exited {code}; see {log_path}')
    return log_path.read_text(encoding='utf-8')


def git_program() -> str:
    """Resolve a native executable, never depot_tools/git.bat with shell=False."""
    configured = os.environ.get('CEF_STATIC_GIT')
    candidate = configured or shutil.which('git.exe' if WINDOWS else 'git')
    if not candidate:
        raise RuntimeError('Native Git executable is missing. The vcpkg port must acquire GIT explicitly.')
    path = Path(candidate)
    if not path.is_absolute() or not path.is_file() or (WINDOWS and path.suffix.lower() != '.exe'):
        raise RuntimeError('CEF_STATIC_GIT must name an existing absolute native Git executable')
    return str(path.resolve())


def git_hash(path: Path) -> str:
    return subprocess.check_output([git_program(), 'rev-parse', 'HEAD'], cwd=path,
                                   text=True).strip()


def python_script(script: Path, *args: str | Path) -> list[str | Path]:
    # Windows vcpkg uses CPython's isolated embeddable distribution. Its _pth
    # intentionally excludes the script directory. Add ONLY that directory;
    # do not edit the interpreter installation or inherit arbitrary PYTHONPATH.
    return [sys.executable, '-c',
            'import pathlib,runpy,sys; '
            'script=str(pathlib.Path(sys.argv[1]).resolve()); '
            'sys.argv=sys.argv[1:]; sys.argv[0]=script; '
            'sys.path.insert(0,str(pathlib.Path(script).parent)); '
            'runpy.run_path(script,run_name="__main__")', script, *args]


def setup_environment(work: Path) -> None:
    git = git_program()
    os.environ['CEF_STATIC_GIT'] = git
    os.environ['DEPOT_TOOLS_UPDATE'] = '0'
    os.environ['DEPOT_TOOLS_WIN_TOOLCHAIN'] = '0'
    os.environ['PYTHONUNBUFFERED'] = '1'
    os.environ['PATH'] = os.pathsep.join((str(work/'depot_tools'),
                                         str(Path(git).parent), os.environ.get('PATH', '')))
    # No GN_DEFINES from an unrelated shell may silently change this recipe.
    os.environ.pop('GN_DEFINES', None)


def transient_network_failure(text: str) -> bool:
    """Transport failures only. Never retry source/hash, compiler or link errors."""
    text = text.lower()
    return any(pattern in text for pattern in (
        'connection reset by peer', 'connection timed out',
        'temporary failure in name resolution', 'remote end hung up unexpectedly',
        'http error 502', 'http error 503', 'http error 504',
        'the requested url returned error: 502',
        'the requested url returned error: 503',
        'the requested url returned error: 504',
    ))


def run_network(command: list[str | Path], cwd: Path, logs: Path, name: str,
                *, timeout: int = 7200, attempts: int = 3) -> str:
    if attempts < 1 or attempts > 3:
        raise ValueError('Network attempts must be between one and three')
    for attempt in range(1, attempts + 1):
        label = f'{name}-{attempt:02d}'
        try:
            return run(command, cwd, logs, label, timeout=timeout)
        except RuntimeError:
            logfile = logs/(label+'.log')
            text = logfile.read_text(encoding='utf-8', errors='replace') if logfile.exists() else ''
            if attempt == attempts or not transient_network_failure(text):
                raise
            print(f'{name}: retrying a recorded transient transport failure; attempt {attempt+1}/{attempts}', flush=True)
            time.sleep(5 * attempt)
    raise AssertionError('unreachable')


def check_tools(work: Path, logs: Path) -> None:
    """Executed with the actual Python/Git and sanitized vcpkg environment."""
    version = run([git_program(), '--version'], work, logs, 'native-git', timeout=30).strip()
    with tempfile.TemporaryDirectory(prefix='cef python toolcheck ', dir=work) as folder:
        directory = Path(folder)
        (directory/'sibling.py').write_text('VALUE = 42\n', encoding='utf-8')
        probe = directory/'probe.py'
        probe.write_text('import sibling,sys\nassert sibling.VALUE == 42\n'
                         'assert sys.argv[1] == "argument with spaces"\n'
                         'print("PYTHON_SIBLING_IMPORT_OK")\n', encoding='utf-8')
        result = run(python_script(probe, 'argument with spaces'), work, logs,
                     'python-script-import', timeout=30)
        if 'PYTHON_SIBLING_IMPORT_OK' not in result:
            raise RuntimeError('Python script import probe failed')
    (logs/'toolchain.json').write_text(json.dumps({
        'git': git_program(), 'git_version': version, 'python': sys.executable,
        'python_version': sys.version, 'sibling_import_verified': True,
        'engine_build_verified': False,
    }, indent=2)+'\n', encoding='utf-8')


def prepare(work: Path, logs: Path) -> Path:
    source = work/'download/chromium/src'
    marker = work/'prepared.json'
    expected = {'cef': CEF, 'chromium': CHROMIUM, 'depot_tools': DEPOT}
    if marker.exists():
        if json.loads(marker.read_text()) != expected:
            raise RuntimeError('Existing workspace belongs to another pinned source set')
        if git_hash(source) != CHROMIUM or git_hash(source/'cef') != CEF:
            raise RuntimeError('Prepared workspace commit mismatch')
        return source
    # This is a capacity precondition, not evidence that a build will fit.
    free = shutil.disk_usage(work).free
    if free < 80 * 1024**3:
        raise RuntimeError(f'Source build needs at least 80 GiB free to start; found {free} bytes')
    depot = work/'depot_tools'
    if not (depot/'.git').is_dir():
        run(['git', 'init', depot], work, logs, 'depot-init')
        run(['git', 'fetch', '--depth=1',
             'https://chromium.googlesource.com/chromium/tools/depot_tools.git', DEPOT],
            depot, logs, 'depot-fetch')
        run(['git', 'checkout', '--detach', 'FETCH_HEAD'], depot, logs, 'depot-checkout')
    if git_hash(depot) != DEPOT:
        raise RuntimeError('depot_tools commit mismatch')
    # DEPOT_TOOLS_UPDATE=0 intentionally prevents gclient.bat from running its
    # updater. Bootstrap the pinned Windows wrappers explicitly instead.
    if WINDOWS:
        run(['cmd', '/c', 'cipd_bin_setup.bat'], depot, logs, 'depot-cipd')
        run(['cmd', '/c', r'bootstrap\win_tools.bat'], depot, logs, 'depot-windows-tools')
        run(['cmd', '/c', 'git.bat', '--version'], depot, logs, 'depot-git-version')
    script = work/'automate-git.py'
    url = f'https://raw.githubusercontent.com/chromiumembedded/cef/{CEF}/tools/automate/automate-git.py'
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != AUTOMATE_SHA256:
        raise RuntimeError('Pinned CEF automation script digest mismatch')
    script.write_bytes(data)
    run_network(python_script(script, f'--download-dir={work / "download"}',
         f'--depot-tools-dir={depot}', '--no-depot-tools-update', '--branch=7977',
         f'--checkout={CEF}', '--no-build', '--no-distrib', '--no-chromium-history',
         '--x64-build'), work, logs, 'source-sync', timeout=7200)
    if git_hash(source) != CHROMIUM or git_hash(source/'cef') != CEF:
        raise RuntimeError('CEF/Chromium commit did not match the lock')
    # automate-git --no-chromium-history may skip hooks on a resumed checkout.
    # Always complete the pinned dependency sync and hooks before certifying
    # preparation; otherwise a recovered transport error could leave tools absent.
    gclient = (['cmd', '/c', str(depot/'gclient.bat')] if WINDOWS else [depot/'gclient'])
    run_network([*gclient, 'sync', '--no-history', '--nohooks'], source.parent,
                logs, 'verify-dependency-sync')
    run(python_script(source/'cef/tools/patcher.py', '--patch-file',
                      source/'cef/patch/patches/runhooks', '--patch-dir', source),
        source/'cef', logs, 'verify-runhooks-patch')
    run_network([*gclient, 'runhooks'], source.parent, logs, 'verify-runhooks')
    if git_hash(source) != CHROMIUM or git_hash(source/'cef') != CEF:
        raise RuntimeError('Source pin changed during dependency verification')
    marker.write_text(json.dumps(expected, indent=2)+'\n')
    (logs/'source-provenance.json').write_text(json.dumps(expected, indent=2)+'\n')
    return source


def find_binary(source: Path, names: list[str]) -> Path:
    for name in names:
        path = source/name
        if path.is_file():
            return path
    raise FileNotFoundError(f'None of the pinned tool locations exists: {names}')


def gn_generate_command(gn: Path, out: Path) -> list[str | Path]:
    # root-target alone still defines unrelated targets from evaluated
    # BUILD.gn files. Restrict the graph to the static executable closure.
    return [gn, 'gen', out, '--fail-on-unused-args',
            '--root-target=//cef:cef_static_smoke',
            '--root-pattern=//cef:cef_static_smoke']


def static_profile(merged: dict, windows: bool) -> dict:
    """Apply only reviewed differences from the upstream distribution profile.

    Do not silently discard arbitrary unused GN arguments. The Linux installer
    argument belongs to a target deliberately excluded by our static root.
    On Windows, Dawn's built DXC target is a DLL, not a static compiler archive.
    Use its supported no-DXC/OS-FXC path, and never search local component DLLs.
    """
    result = dict(merged)
    if windows:
        result.update(dawn_use_built_dxc=False,
                      dawn_force_system_component_load=True,
                      dawn_use_agility_sdk=False)
    else:
        result.pop('enable_linux_installer', None)
    return result


def configuration(source: Path, logs: Path) -> Path:
    cef = source/'cef'
    out = source/'out/CEF_Static_Release_x64'
    recipe = hashlib.sha256((HERE/'patch_source.py').read_bytes()+
                            (HERE/'smoke.c').read_bytes()).hexdigest()
    marker = source/'cef-static-patched.json'
    if marker.exists():
        if json.loads(marker.read_text())['recipe'] != recipe:
            raise RuntimeError('Patched workspace has another recipe; use a fresh workspace')
    else:
        run(python_script(cef/'tools/version_manager.py', '-u', '--fast-check'),
            cef, logs, 'cef-translator')
        run(python_script(cef/'tools/patcher.py'), cef, logs, 'cef-upstream-patches')
        run(python_script(HERE/'patch_source.py', source,
             '--receipt', logs/'source-edits.json'), source, logs, 'static-source-patches')
        (cef/'static').mkdir(exist_ok=True)
        shutil.copy2(HERE/'smoke.c', cef/'static/smoke.c')
        marker.write_text(json.dumps({'recipe': recipe})+'\n')
    spec = importlib.util.spec_from_file_location('cef_gn_args', cef/'tools/gn_args.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = {
        'cef_static_engine': True, 'is_component_build': False,
        'is_official_build': False, 'is_debug': False,
        'symbol_level': 0, 'blink_symbol_level': 0, 'v8_symbol_level': 0,
        'use_thin_lto': False, 'chrome_pgo_phase': 0,
        'use_static_angle': True, 'enable_swiftshader': False,
        'enable_vulkan': False,
        'use_remoteexec': False, 'use_siso': False,
    }
    if not WINDOWS:
        args.update(use_sysroot=True, blink_heap_inside_shared_library=False,
                    ozone_platform_x11=True, ozone_platform_wayland=False)
    if shutil.which('ccache'):
        args['cc_wrapper'] = 'ccache'
    merged = static_profile(module.GetConfigArgs(module.GetMergedArgs(args), False, 'x64'), WINDOWS)
    out.mkdir(parents=True, exist_ok=True)
    (out/'args.gn').write_text(module.GetConfigFileContents(merged)+'\n', newline='\n')
    shutil.copy2(out/'args.gn', logs/'args.gn')
    gn = find_binary(source, ['buildtools/win/gn.exe'] if WINDOWS else ['buildtools/linux64/gn'])
    run(gn_generate_command(gn, out), source, logs, 'gn-gen', timeout=1200)
    # `gn desc` JSON combines data_deps with link dependencies. Walking that
    # union incorrectly treats build-only ANGLE stubs as runtime imports.
    # Keep metadata for the actual link target, then inspect Ninja's link edge.
    graph = run([gn, 'desc', out, '//cef:cef_static_smoke', '--format=json'],
                source, logs, 'gn-graph', timeout=300, quiet=True)
    (logs/'gn-graph.json').write_text(graph)
    ninja = find_binary(source, ['third_party/ninja/ninja.exe'] if WINDOWS else
                        ['third_party/ninja/ninja'])
    executable = 'cef_static_smoke.exe' if WINDOWS else 'cef_static_smoke'
    query = run([ninja, '-C', out, '-t', 'query', executable],
                source, logs, 'native-link-edge', timeout=120, quiet=True)
    # The exporter uses this exact parser too: direct and implicit linker
    # inputs count; order-only generators/data do not. Imported .so/.dll/.TOC
    # dependencies are rejected. Runtime module checks still run after linking.
    export_spec = importlib.util.spec_from_file_location('cef_static_export', HERE/'export_static.py')
    assert export_spec and export_spec.loader
    exporter = importlib.util.module_from_spec(export_spec)
    export_spec.loader.exec_module(exporter)
    inputs = exporter.query_link_inputs(query)
    (logs/'static-link-inputs.json').write_text(json.dumps(inputs, indent=2)+'\n')
    # Audit exportability now, not after a multi-hour native build. No linker
    # script is copied at this stage; the exporter still checks its path later.
    root = json.loads(graph)['//cef:cef_static_smoke']
    flags, omitted = exporter.link_options(root.get('ldflags', []), WINDOWS, lambda p: p)
    (logs/'link-option-audit.json').write_text(json.dumps({
        'schema': 1, 'semantic_link_flags': flags,
        'omitted_build_host_flags': omitted,
        'engine_link_verified': False, 'engine_runtime_verified': False,
    }, indent=2)+'\n')
    return out


def regression_targets(inputs: list[str], windows: bool) -> list[str]:
    names = (('chrome_elf_main.obj', 'initialize_from_primary_module.obj',
              'crash_reporting.obj', 'smoke.obj') if windows else
             ('ozone_image_backing_factory.o', 'smoke.o'))
    result = []
    for name in names:
        candidates = [item for item in inputs if item.endswith('/'+name) or
                      item.endswith('.'+name)]
        if name.startswith('smoke.'):
            candidates = [item for item in candidates if 'cef_static_smoke' in item]
        if len(candidates) != 1:
            raise RuntimeError(f'Expected one regression object for {name}, found {candidates}')
        result.append(candidates[0])
    return result


def compile_regressions(source: Path, out: Path, logs: Path, jobs: int) -> None:
    ninja = find_binary(source, ['third_party/ninja/ninja.exe'] if WINDOWS else
                        ['third_party/ninja/ninja'])
    # Archive members are not direct inputs of the final executable.
    targets = run([ninja, '-C', out, '-t', 'targets', 'all'], source, logs,
                  'native-targets', timeout=120, quiet=True)
    inputs = [line.rsplit(': ', 1)[0] for line in targets.splitlines() if ': ' in line]
    selected = regression_targets(inputs, WINDOWS)
    (logs/'regression-targets.json').write_text(json.dumps(selected, indent=2)+'\n')
    run([ninja, '-C', out, '-j', str(jobs), '-d', 'keeprsp', *selected],
        source, logs, 'regression-compile', timeout=build_timeout())
    receipt = {'schema': 1, 'compiled_objects': selected,
               'engine_link_verified': False, 'engine_runtime_verified': False}
    (logs/'regression-compile-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')


@contextmanager
def hidden_directories(paths: list[Path]):
    """Hide SDK/source roots transactionally; never delete data on restore failure."""
    @contextmanager
    def hide(path: Path):
        container = Path(tempfile.mkdtemp(prefix='.cef-hidden-', dir=path.parent))
        hidden = container/'payload'
        try:
            path.rename(hidden)
        except BaseException:
            container.rmdir()
            raise
        try:
            yield
        finally:
            # If restoring fails, leave the original tree in the container.
            # TemporaryDirectory would delete it here and must not be used.
            hidden.rename(path)
            container.rmdir()
    with ExitStack() as stack:
        for path in paths:
            stack.enter_context(hide(path))
        yield


def verify_binary_imports(imports: str, windows: bool) -> None:
    forbidden = ('libcef.dll', 'libcef.so', 'chrome_elf.dll', 'libegl.dll',
                 'libglesv2.dll', 'libegl.so', 'libglesv2.so', 'libvk_swiftshader',
                 'dxcompiler.dll', 'dxil.dll', 'ffmpeg.dll', 'libffmpeg.so')
    if windows:
        forbidden += ('vcruntime140', 'msvcp140', 'ucrtbase.dll')
    hits = [name for name in forbidden if name in imports.lower()]
    if hits:
        raise RuntimeError('Executable imports forbidden shared dependencies: ' + ', '.join(hits))


def execute_smoke(exe: Path, logs: Path) -> dict:
    # Never accept a proof left by an earlier executable/run.
    (exe.parent/'smoke-result.json').unlink(missing_ok=True)
    command = [exe]
    if not WINDOWS:
        command = ['xvfb-run', '-a', '-s', '-screen 0 1280x1024x24', exe]
    try:
        run(command, exe.parent, logs, 'static-smoke', timeout=120)
    finally:
        if (exe.parent/'cef-static.log').exists():
            shutil.copy2(exe.parent/'cef-static.log', logs/'cef-static.log')
        if (exe.parent/'debug.log').exists():
            shutil.copy2(exe.parent/'debug.log', logs/'debug.log')
    proof = json.loads((exe.parent/'smoke-result.json').read_text())
    required = ['javascript', 'paint', 'browser_modules_clean', 'renderer_modules_clean']
    if proof.get('cef') != '152.0.6+g708dc14+chromium-152.0.7977.83':
        raise RuntimeError('Static engine version does not match the source pin')
    if proof.get('engine') != 'static' or not all(proof.get(k) is True for k in required):
        raise RuntimeError('Incomplete static engine runtime proof')
    if (type(proof.get('browser_pid')) is not int or type(proof.get('renderer_pid')) is not int or
            proof['browser_pid'] <= 0 or proof['renderer_pid'] <= 0 or
            proof['browser_pid'] == proof['renderer_pid']):
        raise RuntimeError('A real separate renderer was not observed')
    shutil.copy2(exe.parent/'smoke-result.json', logs/'smoke-result.json')
    return proof


def compile_and_test(source: Path, work: Path, logs: Path, jobs: int) -> None:
    # An unsuccessful retry must not leave a previous success receipt behind.
    (logs/'engine-build-receipt.json').unlink(missing_ok=True)
    out = configuration(source, logs)
    ninja = find_binary(source, ['third_party/ninja/ninja.exe'] if WINDOWS else ['third_party/ninja/ninja'])
    run([ninja, '-C', out, '-j', str(jobs), '-d', 'keeprsp', 'cef_static_smoke'],
        source, logs, 'engine-build', timeout=build_timeout())
    # Reusing a source workspace must not reuse an old executable/profile.
    deploy = Path(tempfile.mkdtemp(prefix='static-deploy-', dir=work))
    exe = out/('cef_static_smoke.exe' if WINDOWS else 'cef_static_smoke')
    shutil.copy2(exe, deploy/exe.name)
    # Only data, never DLL/SO files. Missing required data must fail the smoke.
    for name in ('icudtl.dat', 'resources.pak', 'chrome_100_percent.pak',
                 'chrome_200_percent.pak', 'snapshot_blob.bin', 'v8_context_snapshot.bin'):
        if (out/name).exists():
            shutil.copy2(out/name, deploy/name)
    if (out/'locales').exists():
        shutil.copytree(out/'locales', deploy/'locales')
    if WINDOWS:
        readobj = find_binary(source, ['third_party/llvm-build/Release+Asserts/bin/llvm-readobj.exe'])
        imports = run([readobj, '--coff-imports', '--coff-load-config', deploy/exe.name],
                      source, logs, 'binary-imports')
    else:
        imports = run(['readelf', '-d', deploy/exe.name], source, logs, 'binary-imports')
        run(['ldd', deploy/exe.name], source, logs, 'runtime-libraries')
    verify_binary_imports(imports, WINDOWS)
    proof = execute_smoke(deploy/exe.name, logs)
    receipt = {'schema': 1, 'cef_commit': CEF, 'chromium_commit': CHROMIUM,
               'depot_tools_commit': DEPOT, 'platform': platform.platform(),
               'configuration': 'Release', 'engine_linkage': 'static',
               'source_build_verified': True, 'sandbox_verified': False,
               'system_libraries_static': False, 'capi_only': True, 'smoke': proof,
               'dxc_enabled': False, 'vulkan_enabled': False,
               'swiftshader_enabled': False, 'system_fxc': WINDOWS,
               'integration_commit': os.environ.get('GITHUB_SHA'),
               'executable_sha256': digest(deploy/exe.name)}
    (logs/'engine-build-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    shutil.rmtree(deploy)  # This directory was created by this invocation only.
    print('STATIC_ENGINE_CAPI_REFERENCE_VERIFIED', flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['tools', 'prepare', 'check', 'regressions', 'build'])
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--logs', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=positive_env('CEF_STATIC_JOBS', 4, 1024))
    args = parser.parse_args()
    if not 1 <= args.jobs <= 1024:
        parser.error('--jobs must be between 1 and 1024')
    build_timeout()  # Validate before downloading or changing source files.
    if platform.machine().lower() not in ('x86_64', 'amd64') or sys.platform not in ('linux', 'win32'):
        raise RuntimeError('Only native Windows/Linux x64 builds are supported')
    work, logs = args.work.resolve(), args.logs.resolve()
    work.mkdir(parents=True, exist_ok=True); logs.mkdir(parents=True, exist_ok=True)
    setup_environment(work)
    if args.phase == 'tools':
        check_tools(work, logs)
        return
    source = prepare(work, logs)
    if args.phase in ('check', 'regressions'):
        out = configuration(source, logs)
        if args.phase == 'regressions':
            # Object targets can depend on almost the entire engine through
            # generated-header/order-only edges. This is NOT a cheap preflight.
            compile_regressions(source, out, logs, args.jobs)
        else:
            (logs/'configuration-receipt.json').write_text(json.dumps({
                'schema': 1, 'phase': 'graph-only',
                'engine_link_verified': False, 'engine_runtime_verified': False,
            }, indent=2)+'\n')
    if args.phase == 'build':
        compile_and_test(source, work, logs, args.jobs)
    print(json.dumps({'phase_completed': args.phase, 'source': str(source)}), flush=True)

if __name__ == '__main__':
    main()
