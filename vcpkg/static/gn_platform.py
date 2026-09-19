#!/usr/bin/env python3
"""Opt-in target-prefix wiring for the pinned Chromium GN pkg_config template.

The default engine-static path and separate host toolchain remain unchanged.
A generated graph is build-input evidence only, never runtime certification.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import platform_contract as contract

CHROMIUM = '79460ebecaa5625e57a5fb679a735659e73dc687'
PKG_CONFIG_PATH = 'build/config/linux/pkg_config.gni'
PKG_CONFIG_BLOB = 'd532180f616efb5c096ff8ebc357baed2b3c7eaa'
SCRIPT_PATH = 'build/config/linux/cef_platform_contract.py'
DIRECT_BLOBS = {
    'printing/BUILD.gn': '39be301bc4d0e16e80deee9b9029d03372919384',
    'media/audio/BUILD.gn': '42f2bcc4ed7bcbb65e7c6e07b8f82b2d8825fd1b',
    'media/midi/BUILD.gn': 'b220567cb68e457a5cd2ec6145bc3d4b08612dab',
    'build/config/linux/dri/BUILD.gn': 'e3a0a83a99fefc27146d7ee4a3096ccea4ddff2f',
}
MARKER = 'cef-static-platform-gn.json'
# The reference link inventory's complete external module set. OS ABI libraries
# are separately allowed; gbm is NOT silently removed while its port is missing.
MODULES = (
    'glib-2.0', 'gmodule-2.0', 'gobject-2.0', 'gthread-2.0', 'gio-2.0',
    'nspr', 'nss', 'atk', 'atk-bridge-2.0', 'atspi-2', 'dbus-1', 'cups',
    'x11', 'xcomposite', 'xdamage', 'xext', 'xfixes', 'xrandr', 'xrender',
    'xtst', 'xi', 'xcb', 'xkbcommon', 'gbm', 'libdrm', 'expat', 'uuid',
    'libpci', 'libudev', 'cairo', 'harfbuzz', 'pango', 'pangocairo',
    'gtk+-3.0', 'alsa', 'zlib', 'xshmfence',
)


def git_blob(data: bytes) -> str:
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def once(text: str, before: str, after: str) -> str:
    contract.require(text.count(before) == 1, 'Pinned GN source differs from reviewed anchor: ' + before[:80])
    return text.replace(before, after, 1)


def patch_template(text: str) -> str:
    """Reviewed transformation of the exact upstream template (tested with GN)."""
    text = once(text, 'declare_args() {\n', '''declare_args() {
  # Experimental static third-party build inputs. Empty preserves upstream.
  cef_static_platform_manifest = ""
  cef_static_platform_prefix = ""
  cef_static_platform_sha256 = ""
''')
    text = once(text, 'template("pkg_config") {\n', '''# Distinct host toolchains retain their ordinary pkg-config/sysroot contract.
_cef_static_target = cef_static_platform_manifest != "" &&
                     current_toolchain == default_toolchain
_cef_static_files = []
if (_cef_static_target) {
  assert(is_linux && current_cpu == "x64" && host_os == "linux")
  assert(!use_sysroot && sysroot == "" && !use_remoteexec)
  assert(pkg_config == "" && host_pkg_config == "",
         "Do not combine the frozen platform contract with another wrapper")
  assert(cef_static_platform_prefix != "" && cef_static_platform_sha256 != "")
  pkg_config_script = "//build/config/linux/cef_platform_contract.py"
  _cef_identity_args = [
    "--manifest", rebase_path(cef_static_platform_manifest),
    "--prefix", rebase_path(cef_static_platform_prefix),
    "--sha256", cef_static_platform_sha256,
  ]
  common_pkg_config_args = [ "query" ] + _cef_identity_args
  pkg_config_args = []
  host_pkg_config_args = []
  _pkg_config_requires_abs_path = true
  _enable_cache = false
  # A dependency change forces GN to run validation again before relinking.
  _cef_static_files = exec_script(pkg_config_script,
                                 [ "inputs" ] + _cef_identity_args, "json",
                                 [ cef_static_platform_manifest ])
  _cef_static_files += [ cef_static_platform_manifest ]
}

template("pkg_config") {
''')
    text = once(text, '      pkgresult = exec_script(pkg_config_script, _script_args, "json")',
                '      pkgresult = exec_script(pkg_config_script, _script_args, "json",\n'
                '                              _cef_static_files)')
    text = once(text, '      lib_dirs = pkgresult[3]\n', '''      lib_dirs = pkgresult[3]
      if (_cef_static_target) {
        ldflags = pkgresult[4]
      }
''')
    return text


def patch_direct(path: str, text: str) -> str:
    if path == 'build/config/linux/dri/BUILD.gn':
        text = once(text, 'pkg_config("dri") {\n',
                    'if (cef_static_platform_manifest != "") {\n'
                    '  # DRI_DRIVER_DIR names dynamic Mesa driver modules. The\n'
                    '  # static profile deliberately carries no runtime DRI .so path.\n'
                    '  config("dri") {}\n'
                    '} else {\n'
                    '  pkg_config("dri") {\n')
        return text + '}\n'
    if path == 'printing/BUILD.gn':
        return once(text, '  if (is_chromeos_device) {\n',
                    '  if (is_chromeos_device || (is_linux && cef_static_platform_manifest != "")) {\n')
    # Both ALSA call sites normally bypass pkg-config entirely. Give them
    # actual target headers, private dependencies and absolute static archives.
    if path == 'media/midi/BUILD.gn':
        text = once(text, 'import("//media/media_options.gni")\n',
                    'import("//media/media_options.gni")\nimport("//build/config/linux/pkg_config.gni")\n')
    text = once(text, '    libs += [ "asound" ]\n',
                '    if (is_linux && cef_static_platform_manifest != "") {\n'
                '      configs += [ ":cef_alsa_static" ]\n'
                '    } else {\n      libs += [ "asound" ]\n    }\n')
    return (text + '\nif (is_linux && use_alsa && cef_static_platform_manifest != "") {\n'
                   '  pkg_config("cef_alsa_static") {\n    packages = [ "alsa" ]\n  }\n}\n')


def inputs(manifest: Path, prefix: Path, sha256: str, *, complete=True) -> dict:
    value = contract.load(manifest, sha256, prefix)
    if complete:
        missing = sorted(set(MODULES) - set(value['modules']))
        contract.require(not missing, 'Missing static CEF modules: ' + ', '.join(missing))
    return value


def bind(source: Path, manifest: Path, prefix: Path, sha256: str) -> dict:
    source = source.resolve(strict=True)
    manifest, prefix = manifest.resolve(strict=True), prefix.resolve(strict=True)
    inputs(manifest, prefix, sha256)
    for path in (source, manifest, prefix):
        contract.require(not any(c in str(path) for c in '\x00\n\r;$"\\'), 'Unsafe GN filesystem path')
    target = source / PKG_CONFIG_PATH
    helper = source / SCRIPT_PATH
    marker = source / MARKER
    module_bytes = Path(contract.__file__).read_bytes()
    descriptor = {
        'schema': 1, 'chromium': CHROMIUM, 'manifest': str(manifest),
        'prefix': str(prefix), 'manifest_sha256': sha256,
        'adapter_sha256': contract.digest(Path(__file__)),
        'query_sha256': hashlib.sha256(module_bytes).hexdigest(),
        'original_gn_blob': PKG_CONFIG_BLOB,
        'runtime_verified': False,
    }
    # Validate every existing binding before touching the checkout. No implicit
    # migration to another prefix/toolchain or an engine-only checkpoint.
    if marker.exists():
        previous = contract.decode(marker.read_bytes())
        expected = dict(descriptor, patched_gn_sha256=contract.digest(target),
                        direct_gn_sha256={p:contract.digest(source/p) for p in DIRECT_BLOBS})
        contract.require(previous == expected and helper.read_bytes() == module_bytes,
                         'Static platform workspace binding changed; use a fresh source workspace')
        return gn_args(manifest, prefix, sha256)
    data = target.read_bytes()
    contract.require(git_blob(data) == PKG_CONFIG_BLOB, 'Unreviewed Chromium pkg_config.gni revision')
    contract.require(not helper.exists(), 'Unowned GN helper already exists')
    edits = {}
    for path, blob in DIRECT_BLOBS.items():
        original = (source/path).read_bytes()
        contract.require(git_blob(original) == blob, 'Unreviewed direct GN dependency config: ' + path)
        edits[path] = patch_direct(path, original.decode('utf-8')).encode('utf-8')
    changed = patch_template(data.decode('utf-8')).encode('utf-8')
    descriptor['patched_gn_sha256'] = hashlib.sha256(changed).hexdigest()
    descriptor['direct_gn_sha256'] = {p:hashlib.sha256(b).hexdigest() for p,b in edits.items()}
    # Marker last: an interrupted write cannot be mistaken for a bound workspace.
    helper.write_bytes(module_bytes)
    target.write_bytes(changed)
    for path, content in edits.items(): (source/path).write_bytes(content)
    marker.write_bytes(contract.canonical(descriptor))
    return gn_args(manifest, prefix, sha256)


def gn_args(manifest: Path, prefix: Path, sha256: str) -> dict:
    return {'cef_static_platform_manifest': str(manifest),
            'cef_static_platform_prefix': str(prefix),
            'cef_static_platform_sha256': sha256,
            'use_sysroot': False, 'use_remoteexec': False, 'use_lld': True,
            'use_vaapi': False, 'use_v4l2_codec': False,
            # Chromium/WebRTC PipeWire support is intentionally implemented
            # through runtime dynamic loading. A fully static engine must not
            # admit that optional runtime .so path; retain X11 capture instead.
            'rtc_use_pipewire': False,
            # CEF does not expose Chrome Remote Desktop as an engine API.
            # On Linux enable_remoting follows use_gtk and would otherwise
            # evaluate remoting/host/linux's runtime-loaded PipeWire path.
            'enable_remoting': False}


def guard(source: Path, selection) -> None:
    if selection is None and (source / MARKER).exists():
        raise ValueError('A static-platform source workspace requires its explicit pinned contract')


def audit_graph(root: dict, source: Path, out: Path, prefix: Path, value: dict) -> dict:
    """GN's final libs may also bypass pkg_config: reject every unbound input."""
    seen = []
    source, out, prefix = (p.resolve() for p in (source, out, prefix))
    allowed = set(value['archive_objects'])
    for library in root.get('libs', []):
        if library in contract.OS_LIBRARIES:
            continue
        if library.startswith('//'):
            path = source/library[2:]
        elif Path(library).is_absolute():
            path = Path(library)
        elif '/' in library:
            path = out/library
        else:
            raise ValueError('Unresolved non-OS GN library bypasses the static contract: ' + library)
        path = path.resolve()
        contract.require(path.suffix == '.a', 'Shared/nonarchive GN library: ' + library)
        if path.is_relative_to(prefix):
            name = path.relative_to(prefix).as_posix()
            contract.require(name in allowed, 'GN linked an uncaptured target archive: ' + library)
            seen.append(name)
        else:
            contract.require(path.is_relative_to(out), 'GN archive outside target prefix or native build: ' + library)
    for directory in root.get('lib_dirs', []):
        path = source/directory[2:] if directory.startswith('//') else out/directory
        contract.require(path.resolve().is_relative_to(out), 'External GN library search directory: ' + directory)
    for flag in root.get('ldflags', []):
        contract.require(not flag.startswith(('-l', '-L', '-Wl,-l', '-Wl,-L')),
                         'Hidden GN library search in ldflags: ' + flag)
    contract.require(seen, 'GN root contains no captured platform archives')
    return {'schema':1, 'status':'static-platform-graph-verified',
            'archives':list(dict.fromkeys(seen)), 'runtime_verified':False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--prefix',type=Path,required=True)
    p.add_argument('--sha256',required=True)
    p.add_argument('--validate-inputs',action='store_true')
    p.add_argument('--verify-graph',type=Path)
    p.add_argument('--out',type=Path)
    args=p.parse_args()
    if bool(args.verify_graph) != bool(args.out):
        p.error('--verify-graph and --out must be supplied together')
    if args.validate_inputs:
        if args.verify_graph or args.out:
            p.error('Input validation cannot be combined with graph verification')
        inputs(args.manifest,args.prefix,args.sha256)
        print(json.dumps({'status':'verified-build-inputs','runtime_verified':False}))
        return
    selected = bind(args.source,args.manifest,args.prefix,args.sha256)
    if args.verify_graph:
        value = inputs(args.manifest,args.prefix,args.sha256)
        graph = contract.decode(args.verify_graph.read_bytes())
        contract.require(isinstance(graph,dict) and '//cef:cef_static_smoke' in graph,
                         'Missing exact CEF root graph')
        result = audit_graph(graph['//cef:cef_static_smoke'], args.source,args.out,args.prefix,value)
        result['manifest_sha256'] = args.sha256
        print(json.dumps(result,indent=2))
    else:
        print(json.dumps(selected,indent=2))

if __name__ == '__main__': main()
