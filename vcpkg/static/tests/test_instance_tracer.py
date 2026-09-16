"""Pinned complete tracer/native allocator-callback regression, not a CEF proof."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('tracer_patch', ROOT/'vcpkg/ports/cef-static/instance_tracer_patch.py')
fix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fix)
PA = 'base/allocator/partition_allocator/src/partition_alloc/'
PINS = {fix.FILE: fix.BLOB,
        PA+'internal_allocator.h': '4e6ed300242114a8c18c072a13d1e7f3c162d66a',
        PA+'internal_allocator_forward.h': '0956089445befdaaf2234b7f6a7d21428d797ce7'}


def fixture_root():
    value = os.environ.get('CEF_PINNED_RUNTIME')
    if not value or not (Path(value)/fix.FILE).is_file():
        if os.environ.get('CEF_REQUIRE_TRACER_NATIVE') == '1':
            raise AssertionError('Mandatory pinned tracer fixtures missing')
        raise unittest.SkipTest('complete pinned tracer fixture is required')
    root = Path(value)
    for name, pin in PINS.items():
        if fix.blob((root/name).read_text(encoding='utf-8')) != pin:
            raise AssertionError('Full upstream fixture blob mismatch: '+name)
    return root


class TracerPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = fixture_root()
        cls.original = (cls.fixture/fix.FILE).read_text(encoding='utf-8')

    def test_exact_minimal_idempotent_source(self):
        expected = self.original
        for old, new in fix.EDITS:
            expected = expected.replace(old, new, 1)
        self.assertEqual(fix.transform(self.original), expected)
        self.assertEqual(fix.transform(expected), expected)
        self.assertEqual(expected.count('PA_CHECK(owner_id)'), 2)
        self.assertIn('std::mutex', expected)
        self.assertNotIn('recursive_mutex', expected)
        self.assertIn('ENABLE_BACKUP_REF_PTR_INSTANCE_TRACER', expected)

    def test_drift_and_partial_changes_rejected(self):
        changed = fix.transform(self.original)
        for text in [self.original+'// drift', changed+'// drift',
                     self.original.replace(*fix.EDITS[0]),
                     self.original+fix.EDITS[3][0], '']:
            with self.subTest(text=text[-32:]), self.assertRaises(RuntimeError):
                fix.transform(text)

    def test_repeat_preserves_timestamps_marker_and_other_object(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root/fix.FILE; path.parent.mkdir(parents=True)
            path.write_text(self.original, encoding='utf-8', newline='\r\n')
            marker = root/'cef-static-patched.json'; marker.write_bytes(b'unchanged-base-marker')
            obj = root/'retained.obj'; obj.write_bytes(b'previous-object')
            clocks = (marker.stat().st_mtime_ns, obj.stat().st_mtime_ns)
            receipt = root/'logs/result.json'
            fix.apply(root, receipt)
            timestamp = path.stat().st_mtime_ns
            fix.apply(root, receipt)
            self.assertEqual(path.stat().st_mtime_ns, timestamp)
            self.assertEqual((marker.stat().st_mtime_ns, obj.stat().st_mtime_ns), clocks)
            self.assertEqual(obj.read_bytes(), b'previous-object')
            self.assertEqual(marker.read_bytes(), b'unchanged-base-marker')
            proof = json.loads(receipt.read_text())
            self.assertEqual(proof['status'], 'already-applied')
            self.assertFalse(proof['engine_runtime_verified'])

    def test_failure_invalidates_receipt_without_writing_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = root/fix.FILE
            path.parent.mkdir(parents=True); path.write_text('drift')
            receipt = root/'result.json'; receipt.write_text('stale')
            with self.assertRaises(RuntimeError): fix.apply(root, receipt)
            self.assertFalse(receipt.exists())
            self.assertEqual(path.read_text(), 'drift')

    def test_actual_isolated_launcher_runs_all_three_source_patches(self):
        driver_spec = importlib.util.spec_from_file_location('tracer_driver', ROOT/'vcpkg/ports/cef-static/source_build.py')
        driver = importlib.util.module_from_spec(driver_spec)
        driver_spec.loader.exec_module(driver)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, original in [(fix.FILE, self.fixture/fix.FILE),
                                   ('base/rand_util.cc', self.fixture/'base/rand_util.cc'),
                                   ('cef/libcef/common/resource_util.cc', ROOT/'libcef/common/resource_util.cc')]:
                target = root/name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
            receipt = root/'logs/runtime.json'
            command = driver.python_script(ROOT/'vcpkg/ports/cef-static/runtime_startup.py',
                                           '--source', root, '--receipt', receipt)
            command.insert(1, '-I')
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertEqual(json.loads(receipt.read_text())['repair'], 'native-startup-v1')
            tracer = json.loads((receipt.parent/'instance-tracer-patch.json').read_text())
            self.assertEqual(tracer['status'], 'applied')
            self.assertEqual(tracer['output_blob'], fix.blob(fix.transform(self.original)))

    def test_driver_applies_before_gn_and_preserves_runtime_guards(self):
        driver = (ROOT/'vcpkg/ports/cef-static/source_build.py').read_text()
        self.assertLess(driver.index("HERE/'runtime_startup.py'"),
                        driver.index('run(gn_generate_command'))
        entry = (ROOT/'vcpkg/ports/cef-static/runtime_startup.py').read_text()
        self.assertIn('instance_tracer_patch.apply(args.source,', entry)
        supervisor = (ROOT/'vcpkg/ports/cef-static/smoke_runtime.py').read_text()
        self.assertIn('120', supervisor)


class NativeTracerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = fixture_root()
        cls.temp = tempfile.TemporaryDirectory(prefix='cef tracer regression ')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.logs = ROOT/'static-diagnostics/instance-tracer-regression'
        cls.logs.mkdir(parents=True, exist_ok=True)
        cls.proof_file = cls.logs/'result.json'; cls.proof_file.unlink(missing_ok=True)
        cls.report = {'fixture_only': True, 'engine_runtime_verified': False,
                      'upstream_blobs': PINS, 'heap_adapter': 'modeled no-hooks PartitionRoot',
                      'real_components': ['full tracer translation unit', 'full InternalAllocator templates',
                                          'C++ containers', 'mutex', 'allocation callbacks'], 'cases': []}
        fixture_files = Path(__file__).parent/'fixtures'
        for name in ('tracer_harness.h', 'tracer_harness.cc'):
            shutil.copy2(fixture_files/name, cls.root/name)
        headers = ['pointers/instance_tracer.h', 'internal/partition_root_internal.h',
                   'partition_alloc_base/check.h', 'partition_alloc_base/debug/stack_trace.h',
                   'partition_alloc_base/no_destructor.h', 'slot_address_and_size.h',
                   'partition_alloc_base/component_export.h', 'partition_alloc_forward.h', 'partition_root.h']
        for name in headers:
            path = cls.root/'partition_alloc'/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#pragma once\n#include "tracer_harness.h"\n')
        for name in ('internal_allocator.h', 'internal_allocator_forward.h'):
            shutil.copy2(source/(PA+name), cls.root/'partition_alloc'/name)
        compiler = shutil.which('cl.exe') if os.name == 'nt' else (shutil.which('clang++') or shutil.which('g++'))
        if not compiler: raise AssertionError('Native compiler is mandatory for tracer regression')
        cls.report['compiler'] = compiler
        cls.exes = {}
        original = (source/fix.FILE).read_text(encoding='utf-8')
        for mode, content in [('original', original), ('fixed', fix.transform(original))]:
            (cls.root/'instance_tracer_under_test.cc').write_text(content, encoding='utf-8')
            exe = cls.root/(mode+('.exe' if os.name == 'nt' else ''))
            if os.name == 'nt':
                args = [compiler, '/nologo', '/std:c++17', '/EHsc', '/MT', '/Od',
                        '/W4', '/WX', '/I'+str(cls.root), 'tracer_harness.cc', '/Fe:'+str(exe)]
                if mode == 'fixed': args += ['/DEXPECT_INTERNAL_ALLOCATOR']
            else:
                args = [compiler, '-std=c++17', '-O0', '-pthread', '-Wall', '-Wextra', '-Werror',
                        '-I'+str(cls.root), 'tracer_harness.cc', '-o', str(exe)]
                if mode == 'fixed': args += ['-DEXPECT_INTERNAL_ALLOCATOR']
            result = subprocess.run(args, cwd=cls.root, capture_output=True, text=True, timeout=60)
            (cls.logs/(mode+'-compile.log')).write_text(result.stdout+result.stderr, encoding='utf-8')
            if result.returncode: raise AssertionError(result.stdout+result.stderr)
            cls.exes[mode] = exe
        cls.record()

    @classmethod
    def record(cls):
        cls.proof_file.write_text(json.dumps(cls.report, indent=2)+'\n', encoding='utf-8')

    def execute(self, mode, case, timeout):
        with (self.logs/(mode+'-'+case+'.log')).open('w', encoding='utf-8') as log:
            try:
                result = subprocess.run([self.exes[mode], case], cwd=self.root,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
                return result.returncode
            except subprocess.TimeoutExpired:
                return 'timeout'  # subprocess.run kills/waits its own single-process fixture

    def test_original_reentry_fails_and_fixed_completes_for_each_callback(self):
        for case in ('free', 'alloc', 'stack', 'snapshot'):
            with self.subTest(case=case):
                control = self.execute('original', case, 2)
                self.assertIn(control, ('timeout', 86), 'original did not reproduce self-deadlock')
                text = (self.logs/('original-'+case+'.log')).read_text()
                self.assertIn('REENTER', text, 'control never reached reentrant callback')
                if control == 86:
                    self.assertIn('EXPECTED_SELF_DEADLOCK', text)
                fixed = self.execute('fixed', case, 10)
                self.assertEqual(fixed, 0)
                self.assertIn('PASS '+case, (self.logs/('fixed-'+case+'.log')).read_text())
                self.report['cases'].append({'case': case, 'original': control, 'fixed_exit': fixed})
                self.record()

    def test_tracking_filtering_duplicates_and_concurrent_access(self):
        for mode in ('original', 'fixed'):
            self.assertEqual(self.execute(mode, 'semantics', 15), 0)
        self.report['cases'].append({'case': 'semantics-and-1800-concurrent-cycles', 'both_exit': 0})
        self.record()


if __name__ == '__main__': unittest.main()
