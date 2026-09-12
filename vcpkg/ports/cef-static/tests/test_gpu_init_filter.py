"""Compile the pinned filter-selection block; stubs are NOT an engine build."""
import ast
import hashlib
import importlib.util
import itertools
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PORT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('filter_patcher', PORT/'patch_source.py')
patcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(patcher)
FILE = 'gpu/ipc/service/gpu_init.cc'
BLOB = '09780f226ffe3794550e40cc6c692167b5c0ca00'
BLOCK = (PORT/'tests/fixtures/gpu-init-filter.inc').read_text()
COMPILER = shutil.which('clang++') or shutil.which('c++')


def patched(text):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); path = root/FILE
        path.parent.mkdir(parents=True); path.write_text(text)
        patcher.patch_gpu_init_filter_set(root)
        return path.read_text()


class FilterGuardTests(unittest.TestCase):
    def test_only_declaration_is_conditionally_compiled(self):
        result = patched(BLOCK)
        old = '  bool filter_set = false;\n#if BUILDFLAG(ENABLE_VULKAN)\n'
        self.assertEqual(result.count('bool filter_set = false;'), 1)
        self.assertEqual(result.count('CHECK(!filter_set)'), 2)
        self.assertIn('BUILDFLAG(SKIA_USE_DAWN) && BUILDFLAG(IS_CHROMEOS)', result)
        self.assertNotIn('[[maybe_unused]] bool', result)
        self.assertNotIn('pragma', result)
        self.assertIn(old, BLOCK)
        self.assertEqual(result[result.index('  if (gpu_feature_info_'):],
                         BLOCK[BLOCK.index('  if (gpu_feature_info_'):])

    def test_source_drift_and_duplicate_application_rejected(self):
        for text in ('', BLOCK*2, patched(BLOCK)):
            with self.subTest(text=text[:10]), self.assertRaises(RuntimeError): patched(text)

    def test_pipeline_and_edit_receipt(self):
        patcher.EDITS.clear(); result = patched(BLOCK)
        self.assertEqual(len(patcher.EDITS), 1)
        self.assertEqual(patcher.EDITS[0]['after_sha256'], hashlib.sha256(result.encode()).hexdigest())
        tree = ast.parse((PORT/'patch_source.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'patch')
        self.assertEqual(sum(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and
            n.func.id == 'patch_gpu_init_filter_set' for n in ast.walk(fn)), 1)

    @unittest.skipUnless(os.environ.get('CEF_PINNED_GPU_INIT'), 'Whole pinned source checked in native regression CI')
    def test_exact_upstream_file_and_context(self):
        data = Path(os.environ['CEF_PINNED_GPU_INIT']).read_bytes()
        self.assertEqual(hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest(), BLOB)
        source = data.decode()
        self.assertEqual(source.count(BLOCK), 1)
        self.assertEqual(patched(source), source.replace(BLOCK, patched(BLOCK), 1))


# Type stubs only; compiler evaluates the unmodified upstream branch bodies.
PRELUDE = '''
#define BUILDFLAG(x) x
#define CHECK(x) ((x) ? (void)0 : failure())
#define DCHECK(x) CHECK(x)
void failure();
enum { GPU_FEATURE_TYPE_VULKAN=0, kGpuFeatureStatusEnabled=1, VK_NULL_HANDLE=0 };
namespace std { template<class T, class... A> T* make_unique(A...); }
template<class T> struct Ptr { T* operator->(); T* get(); explicit operator bool(); };
struct Instance { int vk_instance(); };
struct Vulkan { Instance* GetVulkanInstance(); };
struct Device { int GetAdapter(); };
struct Dawn { Device GetDevice(); };
struct DrmModifiersFilterVulkan {};
struct DrmModifiersFilterDawn {};
struct Factory {
  bool SupportsDrmModifiersFilter();
  template<class T> void SetDrmModifiersFilter(T*);
};
namespace ui {
struct OzonePlatform {
  static OzonePlatform* GetInstance();
  void AfterSandboxEntry();
  Factory* GetSurfaceFactoryOzone();
};
}
struct Probe {
  struct { int status_values[1]; } gpu_feature_info_;
  Ptr<Vulkan> vulkan_implementation_;
  Ptr<Dawn> dawn_context_provider_;
  void run() {
'''


@unittest.skipUnless(COMPILER, 'Native C++ syntax compiler required')
class FilterCompilationTests(unittest.TestCase):
    def compile(self, root, block, flags):
        src = root/'filter.cc'; src.write_text(PRELUDE+block+'\n  }\n};\n')
        command = [COMPILER, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsyntax-only',
                   *[f'-D{k}={v}' for k,v in flags.items()], str(src)]
        return subprocess.run(command, text=True, capture_output=True, timeout=30)

    def test_original_reproduces_linux_warning(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.compile(Path(d), BLOCK, dict(IS_OZONE=1, ENABLE_VULKAN=0,
                                                       SKIA_USE_DAWN=1, IS_CHROMEOS=0))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('filter_set', result.stderr)
            self.assertIn('unused', result.stderr)

    def test_all_sixteen_feature_combinations_with_werror(self):
        with tempfile.TemporaryDirectory() as d:
            for values in itertools.product((0, 1), repeat=4):
                flags = dict(zip(('IS_OZONE','ENABLE_VULKAN','SKIA_USE_DAWN','IS_CHROMEOS'), values))
                with self.subTest(flags=flags):
                    result = self.compile(Path(d), patched(BLOCK), flags)
                    self.assertEqual(result.returncode, 0, result.stdout+result.stderr)


if __name__ == '__main__': unittest.main()
