#!/usr/bin/env python3
"""Freeze native Linux static pkg-config inputs for GN, without system fallback.

This is build-input evidence, not runtime qualification. GN's Linux LLD driver
resolves archive cycles; no libraries are hidden in compiler flags or linker
scripts. The selected files are rechecked, not trusted by their extensions.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import struct
import subprocess
import sys

OS_LIBRARIES = frozenset({'c', 'm', 'dl', 'pthread', 'rt', 'resolv'})
MODULE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]*\Z')
LINK_OPTIONS = frozenset({'-pthread', '-Wl,--export-dynamic'})
KIND = 'linux-x64-static-platform-build-inputs'
# Chromium's GTK3 stubs need <gtk/gtkunixprint.h>, but the corresponding
# pkg-config file contributes no libraries: it only adds an include directory
# and Requires the already captured GTK graph. Keep it outside the 37-module
# link inventory while binding both its metadata and header path to the frozen
# prefix.
HEADER_ONLY_MODULES = {
    'gtk+-unix-print-3.0': {
        'base': 'gtk+-3.0',
        'include': 'include/gtk-3.0/unix-print',
        'pc': 'gtk+-unix-print-3.0.pc',
    },
    'gio-unix-2.0': {
        'base': 'gio-2.0',
        'include': 'include/gio-unix-2.0',
        'pc': 'gio-unix-2.0.pc',
    },
}

# Chromium 152 has exactly two reviewed Linux pkg-config filters in the static
# CEF root: NSS excludes its TLS archive, and Pangocairo excludes FreeType
# because Chromium supplies those implementations itself. Preserve that
# upstream behavior explicitly; every other static filter remains fail-closed.
REVIEWED_FILTERS = frozenset({'-lssl3', 'freetype'})


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def decode(data):
    require(len(data) <= 64 * 1024**2, 'Oversized platform contract')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate JSON field')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def digest(path):
    before = path.stat()
    with path.open('rb') as stream:
        value = hashlib.file_digest(stream, 'sha256').hexdigest()
    after = path.stat()
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
            'Dependency changed while hashing: ' + str(path))
    return value


def member(value):
    require(isinstance(value, str) and value and not any(c in value for c in '\\\x00\n\r;$"'),
            'Unsafe dependency path')
    path = PurePosixPath(value)
    require(not path.is_absolute() and value == path.as_posix() and
            all(p not in ('', '.', '..') for p in value.split('/')), 'Noncanonical dependency path')
    require(path.parts[0] in ('include', 'lib', 'share'), 'Dependency outside package payload')
    return path


def regular(prefix: Path, relative: str, *, directory=False) -> Path:
    relative = member(relative)
    path = prefix
    for component in relative.parts:
        path = path / component
        require(not path.is_symlink(), 'Redirected dependency path: ' + str(path))
    require(path.resolve().is_relative_to(prefix), 'Escaped target prefix')
    mode = path.stat().st_mode
    require(stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode), 'Nonregular dependency: ' + str(path))
    return path


def relative(prefix, value, *, directory=False):
    path = Path(value)
    require(path.is_absolute(), 'pkg-config returned a relative filesystem path')
    try:
        rel = path.relative_to(prefix).as_posix()
    except ValueError as error:
        raise ValueError('pkg-config escaped target prefix: ' + value) from error
    # Normalize ../ from pcfiledir, but never allow a symlink to redirect it.
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'Symlink in dependency path')
    rel = path.resolve().relative_to(prefix).as_posix()
    return regular(prefix, rel, directory=directory).relative_to(prefix).as_posix()


def archive(path):
    """Read every member; reject thin, nested, shared, bitcode and other machines."""
    count = 0
    length = path.stat().st_size
    with path.open('rb') as stream:
        require(stream.read(8) == b'!<arch>\n', 'Thin or invalid static archive: ' + str(path))
        while stream.tell() < length:
            header = stream.read(60)
            require(len(header) == 60 and header[58:] == b'`\n', 'Invalid ar header')
            raw_size = header[48:58].strip()
            require(raw_size.isdigit(), 'Invalid ar member size')
            size = int(raw_size); offset = stream.tell()
            require(offset + size <= length, 'Truncated ar member')
            name = header[:16].rstrip()
            if name not in (b'/', b'//', b'/SYM64/'):
                require(not name.startswith(b'#1/'), 'Unreviewed BSD archive encoding')
                data = stream.read(min(size, 64))
                require(len(data) == 64 and data[:7] == b'\x7fELF\x02\x01\x01'
                        and struct.unpack_from('<HHI', data, 16) == (1, 62, 1),
                        'Non-x64 ELF relocatable object: ' + str(path))
                count += 1
            stream.seek(offset + size)
            if size % 2:
                require(stream.read(1) == b'\n', 'Invalid ar alignment')
    require(count > 0, 'Empty static archive')
    return count


def environment(prefix):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PKG_CONFIG', 'LD_'))}
    env.update(PKG_CONFIG_LIBDIR=os.pathsep.join(str(prefix / p) for p in ('lib/pkgconfig', 'share/pkgconfig')),
               PKG_CONFIG_ALLOW_SYSTEM_LIBS='1', PKG_CONFIG_ALLOW_SYSTEM_CFLAGS='1')
    return env


def flags(prefix: Path, tokens: list[str]) -> dict:
    directories = [prefix / 'lib']
    # A separately owned port may install the selected compiler's static C++
    # runtime here. Never search the compiler's default directories implicitly.
    runtime = prefix / 'lib/cef-toolchain-runtime'
    if runtime.exists():
        regular(prefix, 'lib/cef-toolchain-runtime', directory=True)
        directories.append(runtime)
    result = {'includes': [], 'cflags': [], 'libraries': [], 'link_options': []}
    for token in tokens:
        require(not any(c in token for c in '\x00\n\r;$'), 'Unsafe pkg-config flag')
        if token.startswith('-L'):
            name = relative(prefix, token[2:], directory=True)
            require(member(name).parts[0] == 'lib', 'Library search outside target lib')
            directory = prefix / name
            if directory not in directories: directories.append(directory)
    for token in tokens:
        if token.startswith('-L'):
            continue
        if token.startswith('-I'):
            result['includes'].append(relative(prefix, token[2:], directory=True))
        elif token in LINK_OPTIONS:
            result['link_options'].append(token)
            if token == '-pthread': result['cflags'].append(token)
        elif token.startswith('-D'):
            require(re.fullmatch(r'-D[A-Za-z_][A-Za-z0-9_]*(=[A-Za-z0-9_+.,:/-]+)?', token),
                    'Unreviewed compiler definition')
            result['cflags'].append(token)
        elif token.startswith('-l'):
            name = token[2:]
            require(MODULE_NAME.fullmatch(name), 'Unsupported library syntax')
            if name in OS_LIBRARIES:
                result['libraries'].append(name)
                continue
            candidates = [p / ('lib' + name + '.a') for p in directories if (p / ('lib' + name + '.a')).exists()]
            require(len(candidates) == 1, 'Missing or ambiguous static target archive: ' + name)
            result['libraries'].append(relative(prefix, str(candidates[0])))
        elif os.path.isabs(token):
            rel = relative(prefix, token)
            require(rel.startswith('lib/') and rel.endswith('.a'), 'Nonarchive link input')
            result['libraries'].append(rel)
        else:
            raise ValueError('Unreviewed pkg-config option: ' + token)
    # Preserve first occurrence order; LLD handles cyclic lazy archive members.
    return {k: list(dict.fromkeys(v)) for k, v in result.items()}


def capture(prefix: Path, pkgconf: Path, modules: list[str]) -> dict:
    prefix = prefix.resolve(strict=True); pkgconf = pkgconf.resolve(strict=True)
    require(modules and len(set(modules)) == len(modules) and
            all(MODULE_NAME.fullmatch(n) for n in modules), 'Explicit unique module inventory required')
    entries = {}; paths = set()
    def run(*args):
        text = subprocess.check_output([str(pkgconf), *args], env=environment(prefix), text=True, timeout=120)
        require(len(text) < 1024**2, 'Oversized pkg-config response')
        return text.strip()
    for name in modules:
        entry = flags(prefix, shlex.split(run('--static', '--cflags', '--libs', name)))
        entry['version'] = run('--modversion', name)
        require(re.fullmatch(r'\d+(?:\.\d+)*', entry['version']), 'Unreviewed module version syntax')
        libdir = run('--variable=libdir', name)
        if '\\' in libdir:
            words = shlex.split(libdir)
            require(len(words) == 1, 'Ambiguous pkg-config libdir')
            libdir = words[0]
        entry['libdir'] = relative(prefix, libdir, directory=True)
        entries[name] = entry
        paths.update(p for p in entry['libraries'] if p not in OS_LIBRARIES)
        for includedir in entry['includes']:
            for path in (prefix / includedir).rglob('*'):
                require(not path.is_symlink(), 'Symlink in target include inventory')
                if path.is_file(): paths.add(path.relative_to(prefix).as_posix())
    # Metadata is part of the frozen closure, including private transitive .pc inputs.
    for folder in ('lib/pkgconfig', 'share/pkgconfig'):
        if (prefix / folder).exists():
            regular(prefix, folder, directory=True)
            for path in (prefix / folder).rglob('*.pc'):
                paths.add(path.relative_to(prefix).as_posix())

    # Header-only GN aliases are not link modules, but their metadata and every
    # reachable header byte are still immutable members of the same contract.
    for name, alias in HEADER_ONLY_MODULES.items():
        if alias['base'] not in entries:
            continue
        metadata = [
            path for folder in ('lib/pkgconfig', 'share/pkgconfig')
            for path in (prefix / folder).glob(alias['pc'])
            if path.is_file()
        ]
        require(len(metadata) == 1,
                'Header-only module metadata is not uniquely installed: ' + name)
        paths.add(metadata[0].relative_to(prefix).as_posix())
        directory = regular(prefix, alias['include'], directory=True)
        found = False
        for path in directory.rglob('*'):
            require(not path.is_symlink(), 'Symlink in header-only include inventory')
            if path.is_file():
                found = True
                paths.add(path.relative_to(prefix).as_posix())
        require(found, 'Empty header-only include inventory: ' + name)
    records = {}; counts = {}
    for name in sorted(paths):
        path = regular(prefix, name)
        if name.endswith('.a'): counts[name] = archive(path)
        records[name] = {'size': path.stat().st_size, 'sha256': digest(path)}
    require(counts, 'No target archives')
    return {'schema': 1, 'kind': KIND, 'modules': entries, 'files': records,
            'archive_objects': counts, 'pkgconf': {'sha256': digest(pkgconf), 'version': run('--version')},
            'runtime_verified': False}


def load(manifest: Path, expected: str, prefix: Path, *, full=True) -> dict:
    require(re.fullmatch(r'[0-9a-f]{64}', expected), 'Explicit immutable platform contract hash required')
    require(manifest.is_file() and not manifest.is_symlink(), 'Nonregular platform contract')
    require(manifest.stat().st_size <= 64 * 1024**2, 'Oversized platform contract')
    data = manifest.read_bytes()
    require(hashlib.sha256(data).hexdigest() == expected, 'Platform contract changed')
    value = decode(data)
    require(isinstance(value, dict) and type(value.get('schema')) is int and value['schema'] == 1
            and value.get('kind') == KIND and value.get('runtime_verified') is False, 'Invalid build-input contract')
    require(set(value) == {'schema','kind','modules','files','archive_objects','pkgconf','runtime_verified'}, 'Unknown contract fields')
    require(isinstance(value['modules'], dict) and value['modules'] and isinstance(value['files'], dict)
            and isinstance(value['archive_objects'], dict), 'Missing module or file inventory')
    require(isinstance(value['pkgconf'], dict) and set(value['pkgconf']) == {'version','sha256'}
            and isinstance(value['pkgconf']['version'], str) and value['pkgconf']['version']
            and re.fullmatch(r'[0-9a-f]{64}', str(value['pkgconf']['sha256'])), 'Invalid pkgconf evidence')
    prefix = prefix.resolve(strict=True)
    for name, record in value['files'].items():
        path = regular(prefix, name)
        require(isinstance(record, dict) and set(record) == {'size','sha256'} and type(record['size']) is int
                and record['size'] >= 0 and re.fullmatch(r'[0-9a-f]{64}', str(record['sha256'])), 'Invalid file record')
        require(path.stat().st_size == record['size'], 'Dependency size changed: ' + name)
        if full: require(digest(path) == record['sha256'], 'Dependency bytes changed: ' + name)
    checked = set()
    for name, entry in value['modules'].items():
        require(MODULE_NAME.fullmatch(name) and isinstance(entry, dict)
                and set(entry) == {'includes','cflags','libraries','link_options','version','libdir'}, 'Invalid module entry')
        require(re.fullmatch(r'\d+(?:\.\d+)*', entry['version']), 'Invalid module version')
        for key in ('includes','cflags','libraries','link_options'):
            require(isinstance(entry[key], list) and all(isinstance(s,str) for s in entry[key]), 'Invalid module flags')
        regular(prefix, entry['libdir'], directory=True)
        for path in entry['includes']: regular(prefix,path,directory=True)
        require(all(f in LINK_OPTIONS for f in entry['link_options']), 'Unsafe linker option')
        for flag in entry['cflags']:
            require(flag == '-pthread' or re.fullmatch(r'-D[A-Za-z_][A-Za-z0-9_]*(=[A-Za-z0-9_+.,:/-]+)?',flag), 'Unsafe compiler option')
        for library in entry['libraries']:
            if library in OS_LIBRARIES: continue
            require(library.startswith('lib/') and library.endswith('.a') and library in value['files']
                    and type(value['archive_objects'].get(library)) is int and value['archive_objects'][library] > 0,
                    'Unbound static archive')
            if full and library not in checked:
                require(archive(prefix / library) == value['archive_objects'][library], 'Archive format changed')
                checked.add(library)
    if full:
        # New headers can shadow a previously resolved include without changing
        # any existing file. Bind membership as well as bytes.
        directories = {p for e in value['modules'].values() for p in e['includes']}
        directories.update(
            alias['include'] for alias in HEADER_ONLY_MODULES.values()
            if alias['base'] in value['modules']
        )
        for directory in directories:
            for path in (prefix / directory).rglob('*'):
                require(not path.is_symlink(), 'Redirected include tree')
                if path.is_file():
                    require(path.relative_to(prefix).as_posix() in value['files'], 'Uncaptured target header')
        metadata = {p for folder in ('lib/pkgconfig', 'share/pkgconfig')
                    for p in (prefix / folder).rglob('*.pc')}
        require(all(p.relative_to(prefix).as_posix() in value['files'] for p in metadata), 'Uncaptured pkg-config metadata')
    return value


def query(value: dict, prefix: Path, modules: list[str], patterns=()) -> list:
    includes, cflags, libs, options = [], [], [], []
    for name in modules:
        if name not in value['modules']:
            alias = HEADER_ONLY_MODULES.get(name)
            require(alias is not None, 'GN requested an uncaptured target module: ' + name)
            require(alias['base'] in value['modules'],
                    'Header-only module lacks its captured base: ' + name)
            metadata = [
                path for path in value['files']
                if PurePosixPath(path).name == alias['pc']
            ]
            require(len(metadata) == 1,
                    'Header-only module metadata is not uniquely frozen: ' + name)
            regular(prefix, metadata[0])
            directory = regular(prefix, alias['include'], directory=True)
            members = [
                path.relative_to(prefix).as_posix()
                for path in directory.rglob('*') if path.is_file()
            ]
            require(members and all(path in value['files'] for path in members),
                    'Header-only module bytes are not frozen: ' + name)
            includes.append(str(directory))
            continue
        entry = value['modules'][name]
        includes.extend(str(prefix / path) for path in entry['includes'])
        cflags.extend(entry['cflags'])
        libs.extend(path if path in OS_LIBRARIES else str(prefix/path) for path in entry['libraries'])
        options.extend(entry['link_options'])
    def raw_library(library: str) -> str:
        if library in OS_LIBRARIES:
            return '-l' + library
        name = PurePosixPath(library).name
        if name.startswith('lib') and name.endswith('.a') and len(name) > 5:
            return '-l' + name[3:-2]
        return library

    for pattern in patterns:
        require(pattern in REVIEWED_FILTERS,
                'Unreviewed GN static dependency filter: ' + pattern)
        regex = re.compile(pattern)
        matched = False

        kept_includes = []
        for path in includes:
            if regex.search('-I' + path):
                matched = True
            else:
                kept_includes.append(path)
        includes = kept_includes

        kept_cflags = []
        for flag in cflags:
            if regex.search(flag):
                matched = True
            else:
                kept_cflags.append(flag)
        cflags = kept_cflags

        kept_libs = []
        for library in libs:
            if regex.search(raw_library(library)):
                matched = True
            else:
                kept_libs.append(library)
        libs = kept_libs

        kept_options = []
        for option in options:
            if regex.search(option):
                matched = True
            else:
                kept_options.append(option)
        options = kept_options

        # Chromium's pkg-config.py treats a reviewed -v regexp as a no-op
        # when the selected package graph does not contain a matching flag.
        # This is required for our consolidated cef-nss-static archive: the
        # upstream -lssl3 exclusion remains present in GN, but nss.pc already
        # omits ssl3 entirely. Exact filter names remain fail-closed above.
    return [list(dict.fromkeys(includes)), list(dict.fromkeys(cflags)), list(dict.fromkeys(libs)),
            [], list(dict.fromkeys(options))]


def normalize_gn_cli(argv: list[str]) -> list[str]:
    """Match Chromium pkg-config.py handling of '-v <regexp>' exactly.

    argparse treats a value such as '-lssl3' as a new option, while Chromium's
    optparse consumes it as the value of -v. Normalize only the two reviewed
    upstream filters before parsing; unknown filters remain fail-closed.
    """
    result = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == '-v':
            require(index + 1 < len(argv), 'Missing GN static dependency filter')
            pattern = argv[index + 1]
            require(pattern in REVIEWED_FILTERS,
                    'Unreviewed GN static dependency filter: ' + pattern)
            result.append('-v=' + pattern)
            index += 2
            continue
        result.append(token)
        index += 1
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=('capture','verify','query','inputs'))
    p.add_argument('--prefix',type=Path,required=True)
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--sha256')
    p.add_argument('--pkgconf',type=Path)
    p.add_argument('-v',dest='patterns',action='append',default=[])
    p.add_argument('--atleast-version')
    p.add_argument('--version-as-components',action='store_true')
    p.add_argument('--libdir',action='store_true')
    p.add_argument('modules',nargs='*')
    args = p.parse_intermixed_args(normalize_gn_cli(sys.argv[1:]))
    prefix = args.prefix.resolve(strict=True)
    if args.operation == 'capture':
        require(args.pkgconf is not None and not args.manifest.exists(), 'Explicit pkgconf and fresh manifest required')
        value = capture(prefix,args.pkgconf,args.modules)
        args.manifest.parent.mkdir(parents=True,exist_ok=True)
        args.manifest.write_bytes(canonical(value))
        print(digest(args.manifest))
        return
    value = load(args.manifest,args.sha256 or '',prefix)
    if args.operation == 'inputs':
        print(json.dumps([str(prefix/p) for p in value['files']]))
    elif args.operation == 'verify':
        print(json.dumps({'status':'verified-build-inputs','runtime_verified':False}))
    elif args.atleast_version or args.version_as_components or args.libdir:
        require(len(args.modules)==1 and args.modules[0] in value['modules'], 'Expected one captured module')
        entry=value['modules'][args.modules[0]]
        if args.libdir:
            # Chromium consumes --libdir with exec_script(..., "string").
            # Match upstream pkg-config.py exactly: no trailing newline may
            # enter GN defines such as ATK_LIB_DIR.
            sys.stdout.write(str(prefix/entry['libdir']))
        elif args.version_as_components: print(json.dumps([int(p) for p in entry['version'].split('.')]))
        else:
            require(re.fullmatch(r'\d+(?:\.\d+)*',args.atleast_version), 'Invalid version requirement')
            a=[int(p) for p in entry['version'].split('.')]; b=[int(p) for p in args.atleast_version.split('.')]
            size=max(len(a),len(b)); print(json.dumps(a+[0]*(size-len(a)) >= b+[0]*(size-len(b))))
    else:
        require(args.modules, 'Empty GN module query')
        print(json.dumps(query(value,prefix,args.modules,args.patterns)))


if __name__ == '__main__': main()
