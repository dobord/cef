"""Regression for Ubuntu job 103156443190; GN fixtures are not CEF builds.

The optional native GN test evaluates the actual pinned Dawn block using
explicit dependency stubs. It verifies graph conditions and public include
propagation, NOT compilation of Dawn, libsync, or the complete engine.
"""
from __future__ import annotations
import ast
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

PORT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dawn_patcher', PORT / 'patch_source.py')
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)
FILE = 'gpu/command_buffer/service/BUILD.gn'
BLOB = '09245d2dddb74a8a3817f8490146e42617e6ec44'
# Verbatim relevant block from Chromium 79460ebecaa5625e57a5fb679a735659e73dc687.
BLOCK = '''  if (use_dawn) {
    deps += [
      "//net",
      "//third_party/dawn/src/dawn:proc",
      "//third_party/dawn/src/dawn/native",
      "//third_party/dawn/src/dawn/platform",
    ]
    if (dawn_enable_opengles) {
      sources += [
        "shared_image/dawn_egl_image_representation.cc",
        "shared_image/dawn_egl_image_representation.h",
        "shared_image/dawn_gl_texture_representation.cc",
        "shared_image/dawn_gl_texture_representation.h",
      ]
    }
    if (use_ozone) {
      sources += [
        "shared_image/dawn_ozone_image_representation.cc",
        "shared_image/dawn_ozone_image_representation.h",
      ]
    }
  }
'''
DEPENDENCIES = {
    '//third_party/libsync': 'third_party/libsync/src/include',
    '//third_party/vulkan-headers/src:vulkan_headers': 'third_party/vulkan-headers/src/include',
    '//ui/gfx/linux:drm': 'third_party/libdrm/src/include',
}


def patched(text: str) -> str:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / FILE
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding='utf-8')
        patcher.patch_dawn_ozone_dependencies(root)
        return path.read_text(encoding='utf-8')


def pinned_block() -> str:
    filename = os.environ.get('CEF_PINNED_GPU_BUILD')
    if filename:
        data = Path(filename).read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if blob != BLOB:
            raise AssertionError('Pinned upstream GPU BUILD.gn blob mismatch')
        text = data.decode('utf-8')
        if text.count(BLOCK) != 1:
            raise AssertionError('Pinned upstream Dawn block has drifted')
        # Apply to the WHOLE downloaded upstream file, not just our excerpt.
        actual = patched(text)
        if actual != text.replace(BLOCK, patched(BLOCK), 1):
            raise AssertionError('Patch unexpectedly changes another GN target')
    return BLOCK


class DawnDependencyPatchTests(unittest.TestCase):
    def test_adds_exact_linked_dependencies_not_include_only_workaround(self):
        result = patched(BLOCK)
        for label in DEPENDENCIES:
            self.assertEqual(result.count('"' + label + '"'), 1)
        self.assertIn('if ((is_linux || is_chromeos) && !enable_vulkan)', result)
        self.assertNotIn('include_dirs', result)
        self.assertNotIn('data_deps', result)
        self.assertNotIn('"//gpu/vulkan"', result)

    def test_keeps_dawn_and_sources_enabled(self):
        result = patched(BLOCK)
        before = re.findall(r'"[^"\n]+\.(?:cc|h)"', BLOCK)
        self.assertEqual(re.findall(r'"[^"\n]+\.(?:cc|h)"', result), before)
        self.assertIn('if (use_dawn)', result)
        self.assertNotIn('use_dawn = false', result)
        self.assertNotIn('enable_vulkan = true', result)

    def test_only_the_matching_block_changes(self):
        unrelated = 'group("unrelated") { deps = [ "//third_party/libsync" ] }\n'
        self.assertEqual(patched(unrelated + BLOCK), unrelated + patched(BLOCK))

    def test_drift_duplicate_and_reapplication_fail_closed(self):
        for text in ('', BLOCK + BLOCK, BLOCK.replace('dawn_ozone_', 'different_'), patched(BLOCK)):
            with self.subTest(text=text[:30]), self.assertRaises(RuntimeError):
                patched(text)

    def test_patch_pipeline_calls_the_fix_once(self):
        tree = ast.parse((PORT / 'patch_source.py').read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'patch')
        calls = [n.func.id for n in ast.walk(function)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertEqual(calls.count('patch_dawn_ozone_dependencies'), 1)

    def test_writes_source_edit_provenance(self):
        patcher.EDITS.clear()
        result = patched(BLOCK)
        self.assertEqual(len(patcher.EDITS), 1)
        receipt = patcher.EDITS[0]
        self.assertEqual(receipt['file'], FILE)
        self.assertEqual(receipt['before_sha256'], hashlib.sha256(BLOCK.encode()).hexdigest())
        self.assertEqual(receipt['after_sha256'], hashlib.sha256(result.encode()).hexdigest())

    def test_full_pinned_upstream_context_when_supplied(self):
        self.assertEqual(pinned_block(), BLOCK)


@unittest.skipUnless(shutil.which('gn'), 'Native GN is exercised by dawn-ozone-regression CI')
class DawnDependencyGNTests(unittest.TestCase):
    def test_platform_feature_matrix_and_include_propagation(self):
        before = pinned_block()
        after = patched(before)
        with tempfile.TemporaryDirectory(prefix='cef dawn gn ') as directory:
            root = Path(directory)
            (root / '.gn').write_text('buildconfig = "//BUILDCONFIG.gn"\n')
            (root / 'BUILDCONFIG.gn').write_text('''declare_args() {
  is_linux = false
  is_chromeos = false
  enable_vulkan = false
  use_dawn = true
  use_ozone = true
  dawn_enable_opengles = false
}
set_default_toolchain("//toolchain:host")
''')
            tools = root / 'toolchain'; tools.mkdir()
            (tools / 'BUILD.gn').write_text('''toolchain("host") {
  tool("stamp") { command = "touch {{output}}" }
}
''')
            labels = set(re.findall(r'"(//[^"]+)"', after))
            for label in labels:
                path, _, target = label[2:].partition(':')
                folder = root / path; folder.mkdir(parents=True, exist_ok=True)
                name = target or Path(path).name
                body = 'group("' + name + '") {\n'
                if label in DEPENDENCIES:
                    body += '  public_configs = [ ":headers" ]\n'
                    body += '}\nconfig("headers") {\n'
                    body += '  include_dirs = [ "//' + DEPENDENCIES[label] + '" ]\n'
                (folder / 'BUILD.gn').write_text(body + '}\n')
            comparisons = []
            for platform, vulkan, dawn, ozone in itertools.product(
                    ('linux', 'chromeos', 'other'), (False, True), (False, True), (False, True)):
                values = dict(is_linux=platform == 'linux', is_chromeos=platform == 'chromeos',
                              enable_vulkan=vulkan, use_dawn=dawn, use_ozone=ozone)
                results = []
                for revision, block in (('before', before), ('after', after)):
                    # The copied Dawn block has sources. Clear them because this
                    # fixture tests GN graph semantics, not fake engine objects.
                    (root / 'BUILD.gn').write_text('source_set("probe") {\n'
                        '  deps = []\n  sources = []\n' + block + '  sources = []\n}\n')
                    command = ['gn', 'gen', 'out', '--args=' + ' '.join(
                        k + '=' + str(v).lower() for k, v in values.items())]
                    p = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    p = subprocess.run(['gn', 'desc', 'out', '//:probe', '--format=json'],
                        cwd=root, capture_output=True, text=True, timeout=30)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    results.append(json.loads(p.stdout)['//:probe'])
                old, new = results
                active = platform != 'other' and not vulkan and dawn and ozone
                canonical = lambda label: label if ':' in label else label + ':' + label.rsplit('/', 1)[1]
                added = set(new.get('deps', [])) - set(old.get('deps', []))
                self.assertEqual(added, {canonical(x) for x in DEPENDENCIES} if active else set())
                if active:
                    includes = new.get('include_dirs', [])
                    for include in DEPENDENCIES.values():
                        self.assertIn('//' + include + '/', includes)
                else:
                    self.assertEqual(new.get('deps'), old.get('deps'))
                    self.assertEqual(new.get('include_dirs'), old.get('include_dirs'))
                comparisons.append(dict(values, added_dependencies=sorted(added)))
            evidence = os.environ.get('CEF_DAWN_GN_EVIDENCE')
            if evidence:
                Path(evidence).write_text(json.dumps({
                    'schema': 1, 'upstream_blob': BLOB,
                    'cases': comparisons, 'gn_graph_verified': True,
                    'dependency_targets_are_stubs': True,
                    'cef_engine_built': False, 'native_dawn_compilation_verified': False,
                }, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
