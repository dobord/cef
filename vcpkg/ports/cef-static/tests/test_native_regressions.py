"""Tests for failures in run 34451325586; not an engine build certificate."""
from pathlib import Path
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
PORT = Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location(name, PORT/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
patcher = load('patch_source')
builder = load('source_build')
FILE = 'gpu/command_buffer/service/shared_image/ozone_image_backing_factory.cc'
FACTORY = """int factory(viz::VulkanContextProvider* vulkan_context_provider,
    gfx::BufferUsage usage) {
  scoped_refptr<gfx::NativePixmap> pixmap =
      ui::OzonePlatform::GetInstance()
          ->GetSurfaceFactoryOzone()
          ->CreateNativePixmap(gpu::kNullSurfaceHandle,
                               vulkan_context_provider
                                   ? vulkan_context_provider->GetDeviceQueue()
                                   : nullptr,
                               size, format, usage, size);
  return pixmap;
}
"""
PRELUDE = """
#define BUILDFLAG(flag) flag
struct VulkanDeviceQueue {};
namespace viz {
#if ENABLE_VULKAN
struct VulkanContextProvider {
  VulkanDeviceQueue queue;
  VulkanDeviceQueue* GetDeviceQueue() { return &queue; }
};
#else
struct VulkanContextProvider;
#endif
}
namespace gfx { using BufferUsage=int; struct NativePixmap {}; }
template<class T> using scoped_refptr=int;
namespace gpu { constexpr int kNullSurfaceHandle=0; }
constexpr int size=1, format=2;
namespace ui {
struct OzonePlatform {
  static OzonePlatform* GetInstance() { static OzonePlatform self; return &self; }
  OzonePlatform* GetSurfaceFactoryOzone() { return this; }
  int CreateNativePixmap(int, VulkanDeviceQueue* queue, int, int, int, int) {
    return queue ? 7 : 3;
  }
};
}
"""
class NativeGraphTests(unittest.TestCase):
    def test_static_root_without_disabling_validation(self):
        command = builder.gn_generate_command(Path('gn'), Path('out/native'))
        self.assertIn('--root-target=//cef:cef_static_smoke', command)
        self.assertIn('--root-pattern=//cef:cef_static_smoke', command)
        self.assertIn('--fail-on-unused-args', command)
    def test_static_engine_skips_cefclient_print_pkgconfig(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root/'cef/BUILD.gn'
            target.parent.mkdir(parents=True)
            target.write_text('''    pkg_config("gtk") {
      packages = [
        "gmodule-2.0",
        "gtk+-3.0",
        "gthread-2.0",
        "gtk+-unix-print-3.0",
        "xi",
      ]
    }
''')
            patcher.patch_static_cefclient_pkgconfig(root)
            text = target.read_text()
            self.assertIn('if (!cef_static_engine)', text)
            self.assertEqual(text.count('"gtk+-unix-print-3.0"'), 1)
            self.assertIn('packages += [ "gtk+-unix-print-3.0" ]', text)

    def test_windows_angle_webgpu_uses_nonrelocating_outer_render_target_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root/'third_party/angle/src/libANGLE/renderer/wgpu/TextureWgpu.h'
            target.parent.mkdir(parents=True)
            target.write_text(
                '    using RenderTargetLevels = std::vector<std::deque<RenderTargetWgpu>>;\n'
            )
            patcher.patch_windows_angle_move_only_render_target_container(root)
            text = target.read_text()
            self.assertIn('#if defined(_MSVC_STL_UPDATE)', text)
            self.assertIn(
                'using RenderTargetLevels = std::deque<std::deque<RenderTargetWgpu>>;',
                text,
            )
            self.assertIn(
                'using RenderTargetLevels = std::vector<std::deque<RenderTargetWgpu>>;',
                text,
            )

    def test_windows_angle_webgpu_drops_private_libcpp_config_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root/'third_party/angle/src/libANGLE/renderer/wgpu/FramebufferWgpu.cpp'
            target.parent.mkdir(parents=True)
            target.write_text('''#include "common/unsafe_buffers.h"

#include <__config>

#include "common/Color.h"
''')
            patcher.patch_windows_angle_libcpp_private_include(root)
            text = target.read_text()
            self.assertNotIn('<__config>', text)
            self.assertIn('#include "common/Color.h"', text)

    def test_windows_rrect_stream_operator_gets_direct_ostream_include(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root/'ui/gfx/geometry/rrect_f.h'
            target.parent.mkdir(parents=True)
            target.write_text('''#include <memory>
#include <string>
''')
            patcher.patch_windows_rrect_ostream_include(root)
            self.assertEqual(target.read_text(), '''#include <memory>
#include <ostream>
#include <string>
''')

    def test_windows_cert_util_gets_direct_string_include(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root/'net/tools/transport_security_state_generator/cert_util.h'
            target.parent.mkdir(parents=True)
            target.write_text('''#include <stdint.h>

#include <string_view>
''')
            patcher.patch_windows_missing_string_include(root)
            self.assertEqual(target.read_text(), '''#include <stdint.h>

#include <string>
#include <string_view>
''')

    def test_windows_object_layout(self):
        values = ['obj/chrome/chrome_elf/chrome_elf.chrome_elf_main.obj',
                  'obj/chrome/install_static/secondary_module.initialize_from_primary_module.obj',
                  'obj/cef/libcef_static.crash_reporting.obj',
                  'obj/cef/cef_static_smoke.smoke.obj', 'obj/cef/cef_engine.lib']
        self.assertEqual(builder.regression_targets(values, True), values[:-1])
    def test_linux_object_layout(self):
        values = ['obj/gpu/gles2_sources/ozone_image_backing_factory.o',
                  'obj/cef/cef_static_smoke/smoke.o', 'obj/cef/libcef_engine.a']
        self.assertEqual(builder.regression_targets(values, False), values[:-1])
    def test_missing_or_ambiguous_objects_fail(self):
        with self.assertRaises(RuntimeError): builder.regression_targets([], False)
        with self.assertRaises(RuntimeError):
            builder.regression_targets(['obj/a/ozone_image_backing_factory.o',
                                        'obj/b/ozone_image_backing_factory.o',
                                        'obj/cef_static_smoke/smoke.o'], False)
class VulkanDisabledTests(unittest.TestCase):
    def test_patch_and_drift_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); target = root/FILE
            target.parent.mkdir(parents=True); target.write_text(FACTORY)
            patcher.patch_vulkan_disabled(root)
            self.assertIn('#if BUILDFLAG(ENABLE_VULKAN)', target.read_text())
            self.assertNotIn('? vulkan_context_provider->GetDeviceQueue()', target.read_text())
            with self.assertRaises(RuntimeError): patcher.patch_vulkan_disabled(root)
    @unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
    def test_compiles_without_vulkan_and_preserves_vulkan_behavior(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); target = root/FILE
            target.parent.mkdir(parents=True); target.write_text(FACTORY)
            patcher.patch_vulkan_disabled(root)
            fixture = root/'fixture.cc'
            fixture.write_text(PRELUDE+target.read_text()+"""
int main() {
  if (factory(nullptr, 0) != 3) return 1;
#if ENABLE_VULKAN
  viz::VulkanContextProvider provider;
  if (factory(&provider, 0) != 7) return 2;
#endif
  return 0;
}
""")
            for enabled in (0, 1):
                binary = root/f'test-{enabled}'
                subprocess.run(['c++', '-std=c++17', '-Wall', '-Werror',
                                f'-DENABLE_VULKAN={enabled}', str(fixture),
                                '-o', str(binary)], check=True, capture_output=True)
                subprocess.run([str(binary)], check=True)
if __name__ == '__main__': unittest.main()
