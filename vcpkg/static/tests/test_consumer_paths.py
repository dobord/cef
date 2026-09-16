"""Real CMake consumer path/link regression with a tiny stand-in SDK, not CEF.

Use the production consumer CMakeLists and the same untyped -D arguments as
ci.py. No Chromium checkout, external downloads or completed CEF proof needed.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
CONSUMER = ROOT/'vcpkg/static/consumer'
WINDOWS = os.name == 'nt'


class ConsumerPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cmake = shutil.which('cmake')
        if not cls.cmake:
            raise AssertionError('CMake is required for the consumer regression')
        cls.tmp = tempfile.TemporaryDirectory(prefix='cef consumer paths ')
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.logs = ROOT/'static-diagnostics/consumer-path-regression'
        cls.logs.mkdir(parents=True, exist_ok=True)
        (cls.logs/'result.json').unlink(missing_ok=True)
        cls.sequence = 0
        cls.generator = (['-G', 'Visual Studio 17 2022', '-A', 'x64'] if WINDOWS
                         else ['-G', 'Unix Makefiles', '-DCMAKE_BUILD_TYPE=Release'])
        cls.report = {'fixture_only': True, 'engine_runtime_verified': False,
                      'host': os.name, 'cmake': cls.cmake, 'cases': [],
                      'consumer_sha256': hashlib.sha256((CONSUMER/'CMakeLists.txt').read_bytes()).hexdigest()}
        cls.command(['--version'], 'cmake-version')
        producer = cls.root/'producer'; producer.mkdir()
        (producer/'fixture.h').write_text('int fixture_answer(void);\n')
        (producer/'fixture.c').write_text('#include "fixture.h"\nint fixture_answer(void) { return 42; }\n')
        (producer/'fixture.dat').write_bytes(b'K')
        (producer/'cef-static-config.cmake').write_text('''include("${CMAKE_CURRENT_LIST_DIR}/cef-static-targets.cmake")
function(cef_static_deploy_resources target)
    get_filename_component(prefix "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/../.." ABSOLUTE)
    add_custom_command(TARGET ${target} POST_BUILD
        COMMAND "${CMAKE_COMMAND}" -E copy_if_different
        "${prefix}/share/cef-static/fixture.dat" "$<TARGET_FILE_DIR:${target}>/fixture.dat"
        VERBATIM)
endfunction()
''')
        (producer/'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.24)
project(consumer_path_fixture LANGUAGES C)
add_library(fixture STATIC fixture.c)
set_target_properties(fixture PROPERTIES EXPORT_NAME static MSVC_RUNTIME_LIBRARY MultiThreaded)
target_include_directories(fixture INTERFACE "$<INSTALL_INTERFACE:include>")
install(TARGETS fixture EXPORT fixture_targets ARCHIVE DESTINATION lib)
install(EXPORT fixture_targets FILE cef-static-targets.cmake NAMESPACE CEF:: DESTINATION share/cef-static)
include(CMakePackageConfigHelpers)
write_basic_package_version_file("${CMAKE_CURRENT_BINARY_DIR}/cef-static-config-version.cmake"
    VERSION 152.0.6 COMPATIBILITY ExactVersion)
install(FILES cef-static-config.cmake "${CMAKE_CURRENT_BINARY_DIR}/cef-static-config-version.cmake" fixture.dat
    DESTINATION share/cef-static)
install(FILES fixture.h DESTINATION include)
''')
        build = producer/'build'; initial = cls.root/'initial sdk'
        cls.command(['-S', str(producer), '-B', str(build), *cls.generator], 'producer-configure')
        cls.command(['--build', str(build), '--config', 'Release'], 'producer-build')
        cls.command(['--install', str(build), '--config', 'Release', '--prefix', str(initial)], 'producer-install')
        cls.sdk = cls.root/'relocated sdk'; initial.rename(cls.sdk)
        shutil.rmtree(producer)
        cls.source = cls.root/'a source/new files/test/smoke.c'
        cls.source.parent.mkdir(parents=True)
        cls.source.write_text('''#include "fixture.h"
#include <stdio.h>
int main(void) {
    FILE* data = fopen("fixture.dat", "rb");
    int value;
    if (!data) return 2;
    value = fgetc(data);
    if (fclose(data) != 0) return 3;
    return fixture_answer() == 42 && value == 'K' ? 0 : 4;
}
''')
        cls.report['producer_tree_removed'] = not producer.exists()
        cls.record()

    @classmethod
    def command(cls, args, name, *, succeeds=True):
        cls.sequence += 1
        result = subprocess.run([cls.cmake, *args], cwd=cls.root, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=120)
        (cls.logs/f'{cls.sequence:02d}-{name}.log').write_text(result.stdout+result.stderr, encoding='utf-8')
        if succeeds and result.returncode:
            raise AssertionError(result.stdout+result.stderr)
        return result

    @classmethod
    def record(cls):
        (cls.logs/'result.json').write_text(json.dumps(cls.report, indent=2)+'\n', encoding='utf-8')

    def configure(self, value, label, *, succeeds=False):
        build = self.root/('consumer-'+label)
        args = ['-S', str(CONSUMER), '-B', str(build), *self.generator,
                '-DCMAKE_PREFIX_PATH='+str(self.sdk),
                '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF',
                '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF']
        if value is not None:
            args.append('-DCEF_STATIC_SMOKE_SOURCE='+value)
        return build, self.command(args, label+'-configure', succeeds=succeeds)

    def check_consumer(self, path, label):
        build, _ = self.configure(path, label, succeeds=True)
        self.command(['--build', str(build), '--config', 'Release'], label+'-build')
        executable = build/'Release/cef_static_smoke.exe' if WINDOWS else build/'cef_static_smoke'
        self.assertTrue(executable.is_file())
        hidden = self.sdk.with_name('hidden sdk')
        self.sdk.rename(hidden)
        try:
            result = subprocess.run([str(executable)], cwd=executable.parent, capture_output=True,
                                    text=True, timeout=30)
        finally:
            hidden.rename(self.sdk)
        (self.logs/(label+'-run.log')).write_text(result.stdout+result.stderr, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual((executable.parent/'fixture.dat').read_bytes(), b'K')
        self.report['cases'].append({'case': label, 'source_argument': path, 'exit_code': 0,
                                     'sdk_hidden_during_run': True})
        self.record()

    def test_native_separators_with_spaces(self):
        path = str(self.source)
        if WINDOWS:
            self.assertIn('\\a source\\new files\\test\\', path)
        self.check_consumer(path, 'native-separators')

    def test_cmake_separators_with_spaces(self):
        self.check_consumer(self.source.as_posix(), 'cmake-separators')

    def test_legacy_windows_argument_reproduces_exact_escape_error(self):
        legacy = self.root/'legacy'; legacy.mkdir()
        (legacy/'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.24)
project(legacy_consumer LANGUAGES C)
add_executable(smoke "${CEF_STATIC_SMOKE_SOURCE}")
''')
        result = self.command(['-S', str(legacy), '-B', str(legacy/'build'), *self.generator,
            r'-DCEF_STATIC_SMOKE_SOURCE=D:\a\cef\cef\vcpkg\ports\cef-static\smoke.c'],
            'legacy-escape-control', succeeds=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid character escape '\\a'", result.stdout+result.stderr)
        self.report['cases'].append({'case': 'legacy-escape-control', 'expected_failure': True})
        self.record()

    def test_missing_and_empty_source_rejected(self):
        for label, value in [('undefined', None), ('empty', '')]:
            with self.subTest(label=label):
                _, result = self.configure(value, label)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Supply CEF_STATIC_SMOKE_SOURCE', result.stdout+result.stderr)

    def test_non_file_and_relative_source_rejected(self):
        for label, value in [('missing', str(self.root/'missing.c')),
                             ('directory', str(self.source.parent)), ('relative', 'smoke.c')]:
            with self.subTest(label=label):
                _, result = self.configure(value, label)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('must name an existing absolute source file', result.stdout+result.stderr)

    @unittest.skipUnless(WINDOWS, 'MSVC-specific ISO C stdio deprecation control')
    def test_msvc_portable_stdio_requires_the_scoped_definition(self):
        legacy = self.root/'legacy-stdio'; legacy.mkdir()
        text = (CONSUMER/'CMakeLists.txt').read_text()
        definition = '    target_compile_definitions(cef_static_smoke PRIVATE _CRT_SECURE_NO_WARNINGS)\n'
        self.assertEqual(text.count(definition), 1)
        (legacy/'CMakeLists.txt').write_text(text.replace(definition, ''))
        build = legacy/'build'
        self.command(['-S', str(legacy), '-B', str(build), *self.generator,
                      '-DCMAKE_PREFIX_PATH='+str(self.sdk),
                      '-DCEF_STATIC_SMOKE_SOURCE='+str(self.source)], 'legacy-stdio-configure')
        result = self.command(['--build', str(build), '--config', 'Release'],
                              'legacy-stdio-build', succeeds=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('C4996', result.stdout+result.stderr)
        self.report['cases'].append({'case': 'legacy-stdio-control', 'expected_failure': True})
        self.record()

    def test_path_list_is_not_silently_split(self):
        _, result = self.configure(self.source.as_posix()+';other.c', 'path-list')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must be one literal file path', result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
