"""Stage an isolated consumer-built C++ client for the static C-ABI engine.

The engine contains its own C++ API implementations. The usual wrapper has
identical symbols and can bind to those incompatible implementations. Keep
upstream's generated C-ABI translations, but isolate client C++ identities.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
import shutil

NAMES = re.compile(r'\bCef[A-Z][A-Za-z0-9_]*\b')
MACROS = re.compile(r'^\s*#\s*define\s+(Cef[A-Z][A-Za-z0-9_]*)', re.M)
# Comments, literals and include paths are not C++ identifiers.
TOKENS = re.compile(
    r'//[^\n]*|/\*.*?\*/|R"(?P<delimiter>[^\s()\\"]{0,16})\(.*?\)(?P=delimiter)"|'
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
    r'\bnamespace\s+(?:base|cef|shutdown_checker)\b|'
    r'\b(?:base|cef|shutdown_checker)\b(?=\s*::)', re.S)
NAMESPACES = {'base': 'cef_static_client_base', 'cef': 'cef_static_client',
              'shutdown_checker': 'cef_static_client_shutdown_checker'}


def isolate_namespaces(text: str) -> str:
    def replace(match):
        token = match[0]
        if token.startswith('namespace'):
            return re.sub(r'\b(base|cef|shutdown_checker)\b',
                          lambda name: NAMESPACES[name[0]], token)
        return NAMESPACES.get(token, token)
    return TOKENS.sub(replace, text)


def stage(source: Path, prefix: Path) -> None:
    """Use translated sources and generated headers from this actual build."""
    variables = {}
    for name in ('cef_paths.gypi', 'cef_paths2.gypi'):
        variables.update(ast.literal_eval((source/name).read_text())['variables'])
    keys = ('libcef_dll_wrapper_sources_base',
            'libcef_dll_wrapper_sources_common', 'autogen_client_side',
            'libcef_dll_wrapper_sources_win')
    groups = {}
    for key in keys:
        groups[key] = sorted({name for name in variables[key] if name.endswith('.cc')})
        if not groups[key]:
            raise ValueError('Empty upstream C++ client source group: '+key)
    root = prefix/'share/cef-static/cpp-client'
    headers = prefix/'include/cef-static-cpp'
    if root.exists() or headers.exists():
        raise ValueError('C++ client staging destination must be new')
    root.mkdir(parents=True)
    shutil.copytree(source/'libcef_dll', root/'libcef_dll')
    shutil.copytree(prefix/'include/cef-static/include', headers/'include')
    compiler = headers/'include/base/cef_compiler_specific.h'
    text = compiler.read_text(encoding='utf-8')
    attribute = '#define TRIVIAL_ABI [[clang::trivial_abi]]'
    if text.count(attribute) != 1:
        raise ValueError('Pinned client trivial_abi declaration changed')
    # Client value types must have the ordinary non-trivial C++ calling
    # convention in BOTH GCC and Clang. The engine's original header remains
    # unchanged; its refptr objects never cross this C ABI boundary.
    compiler.write_text(text.replace(attribute, '#define TRIVIAL_ABI'), encoding='utf-8')
    names, macros = set(), set()
    for tree in (root/'libcef_dll', headers/'include'):
        for path in sorted(tree.rglob('*')):
            if path.is_file() and path.suffix in ('.h', '.cc', '.inc'):
                text = path.read_text(encoding='utf-8')
                names.update(NAMES.findall(text))
                macros.update(MACROS.findall(text))
                path.write_text(isolate_namespaces(text), encoding='utf-8')
    # Function/type macros already expand to C ABI operations/types.
    names -= macros
    if not {'CefExecuteProcess', 'CefInitialize', 'CefBrowserHost', 'CefApp'} <= names:
        raise ValueError('Incomplete public C++ client identity')
    (headers/'include/cef_static_client_names.h').write_text(
        '// Generated static client identities. C API names are unchanged.\n'
        '#pragma once\nnamespace cef_static_client_base {}\n'
        'namespace base = cef_static_client_base;\n'
        'namespace cef_static_client {}\nnamespace cef = cef_static_client;\n'
        + ''.join(f'#define {name} CefStaticClient{name[3:]}\n'
                  for name in sorted(names)), encoding='utf-8')
    cmake = []
    for key, files in groups.items():
        cmake.append('set('+key)
        for name in files:
            if not (root/name).is_file():
                raise ValueError('Missing translated C++ client source: '+name)
            cmake.append('  "${_cef_client_source}/'+name+'"')
        cmake.append(')')
    (root/'sources.cmake').write_text('\n'.join(cmake)+'\n', encoding='utf-8')
