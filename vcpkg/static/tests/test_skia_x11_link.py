"""Viz GN source selection regression; not a full CEF runtime test."""
from __future__ import annotations
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('x11_link_patch', ROOT/'vcpkg/ports/cef-static/skia_x11_link.py')
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)


def active_strings(text, values):
    """Evaluate only the literal/if subset in the reviewed block, not arbitrary GN."""
    active = [True]
    found = []
    for line in text.splitlines():
        value = line.strip()
        if value.startswith('if ('):
            expression = value[4:value.rfind(')')]
            if re.sub(r'\b(enable_vulkan|skia_use_dawn|ozone_platform_x11|is_android|is_chromeos|use_v4l2_codec)\b|[()|&! ]', '', expression):
                raise AssertionError('Unexpected fixture expression')
            for name, enabled in values.items():
                expression = re.sub(r'\b'+name+r'\b', str(bool(enabled)), expression)
            expression = expression.replace('&&', ' and ').replace('||', ' or ').replace('!', ' not ')
            active.append(active[-1] and eval(expression, {'__builtins__': {}}, {}))
        elif value == '}':
            active.pop()
        elif active[-1]:
            found.extend(re.findall(r'"([^"]+)"', value))
    return found


class SourceTests(unittest.TestCase):
    def test_exact_union_selects_x11_sources_and_dependencies_once(self):
        for v, d, x in itertools.product((False, True), repeat=3):
            flags = dict(enable_vulkan=v, skia_use_dawn=d, ozone_platform_x11=x,
                         is_android=False, is_chromeos=False, use_v4l2_codec=False)
            for text, expected in ((fix.OLD, v and x), (fix.NEW, (v or d) and x)):
                selected = active_strings(text+'  }\n', flags)
                for entry in ('display_embedder/skia_output_device_x11.cc',
                              'display_embedder/skia_output_device_x11.h', 'xshmfence',
                              '//ui/base/x', '//ui/events/platform/x11', '//ui/gfx/x'):
                    with self.subTest(flags=flags, entry=entry, fixed=text==fix.NEW):
                        self.assertEqual(selected.count(entry), int(expected))
                self.assertEqual(selected.count('//gpu/vulkan'), int(v))

    def test_vulkan_android_chromeos_behavior_is_unchanged(self):
        for v, a, c, codec in itertools.product((False, True), repeat=4):
            flags = dict(enable_vulkan=v, skia_use_dawn=False, ozone_platform_x11=False,
                         is_android=a, is_chromeos=c, use_v4l2_codec=codec)
            self.assertEqual(active_strings(fix.OLD+'  }\n', flags), active_strings(fix.NEW+'  }\n', flags))

    def test_changes_no_implementation_or_backend_flags(self):
        self.assertEqual(fix.NEW.count('skia_output_device_x11.cc'), 1)
        self.assertNotIn('enable_vulkan =', fix.NEW)
        self.assertNotIn('skia_use_dawn =', fix.NEW)
        self.assertIn('    deps += [ "//gpu/vulkan" ]', fix.NEW)

    def test_unreviewed_full_file_and_duplicate_context_are_rejected(self):
        for text in ('unreviewed', fix.OLD, fix.OLD*2, fix.NEW*2):
            with self.subTest(text=text[:30]), self.assertRaisesRegex(RuntimeError, 'context mismatch'):
                fix.transform(text)

    def test_minimal_idempotent_transform_preserves_cef_osr_additions(self):
        text = '// fixture\n"//cef/libcef/browser/osr/software_output_device_proxy.cc"\n'+fix.OLD+'  }\n'
        with patch.object(fix, 'INPUT_BLOBS', {fix.blob(text)}):
            changed, original = fix.transform(text)
            self.assertEqual(changed, text.replace(fix.OLD, fix.NEW, 1))
            self.assertIn('software_output_device_proxy.cc', changed)
            self.assertEqual(fix.transform(changed), (changed, original))
            with self.assertRaises(RuntimeError): fix.transform(changed+'// unexpected edit\n')

    def test_receipt_and_object_clocks_and_repeat_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'src'; target = source/fix.FILE
            target.parent.mkdir(parents=True); target.write_text(fix.OLD)
            obj = source/'keep.o'; obj.write_bytes(b'compiled fixture'); old = obj.stat().st_mtime_ns
            receipt = root/'logs/patch.json'
            with patch.object(fix, 'INPUT_BLOBS', {fix.blob(fix.OLD)}):
                fix.apply(source, receipt)
                clock = target.stat().st_mtime_ns
                fix.apply(source, receipt)
            self.assertEqual(clock, target.stat().st_mtime_ns)
            self.assertEqual(obj.stat().st_mtime_ns, old)
            self.assertEqual(obj.read_bytes(), b'compiled fixture')
            data = json.loads(receipt.read_text())
            self.assertEqual(data['status'], 'already-applied')
            self.assertFalse(data['engine_runtime_verified'])

    def test_failed_patch_removes_stale_receipt_without_changing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = root/fix.FILE; target.parent.mkdir(parents=True)
            target.write_text('unknown'); receipt = root/'receipt.json'; receipt.write_text('old success')
            with self.assertRaises(RuntimeError): fix.apply(root, receipt)
            self.assertFalse(receipt.exists()); self.assertEqual(target.read_text(), 'unknown')

    def test_source_recipe_kept_and_supplement_runs_before_gn(self):
        build = (ROOT/'vcpkg/ports/cef-static/source_build.py').read_text()
        self.assertIn("(HERE/'patch_source.py').read_bytes()+", build)
        self.assertLess(build.index("HERE/'skia_x11_link.py'"), build.index('run(gn_generate_command'))
        self.assertIn("'viz-x11-graph-check'", build)

    @unittest.skipUnless(os.environ.get('CEF_PINNED_VIZ_GN'), 'complete pinned Viz GN supplied by native configuration')
    def test_complete_pinned_file(self):
        text = Path(os.environ['CEF_PINNED_VIZ_GN']).read_text()
        changed, sha = fix.transform(text)
        self.assertIn(sha, fix.INPUT_BLOBS)
        self.assertEqual(fix.transform(changed), (changed, sha))


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.graph = {fix.TARGET: {
            'sources': ['//components/viz/service/display_embedder/skia_output_device_x11.cc'],
            'libs': ['xshmfence'], 'deps': ['//ui/base/x:x','//ui/gfx/x:x','//ui/events/platform/x11:x11']}}

    def test_real_graph_contract_not_runtime_proof(self):
        fix.verify_graph(self.graph)

    def test_missing_duplicate_object_or_missing_library_dependency_fails(self):
        for field in ('sources','libs','deps'):
            graph = json.loads(json.dumps(self.graph));graph[fix.TARGET][field] = []
            with self.subTest(field=field), self.assertRaises(RuntimeError): fix.verify_graph(graph)
        self.graph[fix.TARGET]['sources'] *= 2
        with self.assertRaises(RuntimeError): fix.verify_graph(self.graph)

    def test_wrong_target_cannot_pass(self):
        with self.assertRaises(RuntimeError): fix.verify_graph({})


if __name__ == '__main__': unittest.main()
