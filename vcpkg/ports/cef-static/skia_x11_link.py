#!/usr/bin/env python3
"""Include the existing X11 output device for Dawn as well as Vulkan.

Run after upstream CEF patches and before GN generation on fresh and resumed
sources. This supplements, rather than repeats, the base source patch recipe.
"""
from __future__ import annotations
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import tempfile

FILE = 'components/viz/service/BUILD.gn'
TARGET = '//components/viz/service:service'
# Chromium 79460ebe before and after CEF's pinned viz_osr_2575.patch.
INPUT_BLOBS = frozenset({'ae70acf5713bc7a0506e3ffbc02ed4b039e980af',
                         '88d42a04b6f6923f9d50e18052aacddd7a684e7f'})
X11_BLOCK = '''    # TODO(crbug.com/40193891): ideally, ozone_platform_x11 should not be used
    # outside
    # ui/ozone.
    if (ozone_platform_x11) {
      sources += [
        "display_embedder/skia_output_device_x11.cc",
        "display_embedder/skia_output_device_x11.h",
      ]

      libs = [ "xshmfence" ]

      deps += [
        "//ui/base/x",
        "//ui/events/platform/x11",
        "//ui/gfx/x",
      ]
    }

'''
OLD = '''  if (enable_vulkan) {
    deps += [ "//gpu/vulkan" ]

    sources += [
      "display_embedder/skia_output_device_vulkan.cc",
      "display_embedder/skia_output_device_vulkan.h",
    ]

    if (is_android) {
      sources += [
        "display_embedder/skia_output_device_vulkan_secondary_cb.cc",
        "display_embedder/skia_output_device_vulkan_secondary_cb.h",
      ]
    }

''' + X11_BLOCK + '''    if (is_chromeos && use_v4l2_codec) {
      deps += [ "//media/gpu/chromeos:common" ]
    }
  }

  if (skia_use_dawn) {
'''
# Preserve the original source/dependency/library lists and Vulkan branches.
# The block moves outside Vulkan; only its predicate widens to the union.
OUTER_X11 = ''.join(line[2:] if line.startswith('  ') else line
                    for line in X11_BLOCK.splitlines(keepends=True))
OUTER_X11 = OUTER_X11.replace('if (ozone_platform_x11)',
    'if ((enable_vulkan || skia_use_dawn) && ozone_platform_x11)', 1)
NEW = OLD.replace(X11_BLOCK, '', 1).replace(
    '  if (skia_use_dawn) {\n', OUTER_X11 + '  if (skia_use_dawn) {\n', 1)


def blob(text: str) -> str:
    data = text.encode('utf-8')
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def transform(text: str) -> tuple[str, str]:
    """Accept only an exact reviewed file, or our exact idempotent result."""
    already = text.count(NEW) == 1 and OLD not in text
    original = text.replace(NEW, OLD, 1) if already else text
    if blob(original) not in INPUT_BLOBS or original.count(OLD) != 1:
        raise RuntimeError('Pinned Viz GN source/context mismatch; review before patching')
    return (text if already else text.replace(OLD, NEW, 1)), blob(original)


def apply(source: Path, receipt: Path) -> None:
    path = source / FILE
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.unlink(missing_ok=True)
    if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
        raise RuntimeError('Refusing a redirected Viz GN source')
    text = path.read_text(encoding='utf-8')
    changed, original_blob = transform(text)
    if changed != text:
        fd, temp = tempfile.mkstemp(prefix='.cef-x11-', suffix='.gn', dir=path.parent)
        temporary = Path(temp)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(changed)
            temporary.chmod(path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    receipt.write_text(json.dumps({
        'schema': 1, 'migration': 'skia-dawn-x11-link-v1', 'file': FILE,
        'status': 'already-applied' if changed == text else 'applied',
        'reviewed_input_blob': original_blob,
        'before_sha256': hashlib.sha256(text.encode()).hexdigest(),
        'after_sha256': hashlib.sha256(changed.encode()).hexdigest(),
        'diff': ''.join(difflib.unified_diff(text.splitlines(True), changed.splitlines(True),
                                            fromfile='a/'+FILE, tofile='b/'+FILE)),
        'engine_link_verified': False, 'engine_runtime_verified': False,
    }, indent=2)+'\n', encoding='utf-8')


def verify_graph(graph: dict) -> None:
    """Validate the actual native Linux GN graph before the expensive build."""
    target = graph.get(TARGET, {})
    sources, deps, libs = (target.get(key, []) for key in ('sources', 'deps', 'libs'))
    required = '//components/viz/service/display_embedder/skia_output_device_x11.cc'
    required_deps = {'//ui/base/x', '//ui/events/platform/x11', '//ui/gfx/x'}
    if (sources.count(required) != 1 or 'xshmfence' not in libs
            or not required_deps.issubset({d.split(':')[0] for d in deps})):
        raise RuntimeError('Dawn X11 implementation or link dependencies missing from native GN graph')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--verify-graph', type=Path)
    args = parser.parse_args()
    if args.verify_graph:
        verify_graph(json.loads(args.verify_graph.read_text(encoding='utf-8')))
    elif args.source and args.receipt:
        apply(args.source, args.receipt)
    else:
        parser.error('Pass --source and --receipt, or --verify-graph')


if __name__ == '__main__':
    main()
