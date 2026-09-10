"""Real GN option fixtures are configuration evidence, not a runtime certificate."""
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('exporter', HERE.parent/'export_static.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

class ObservedLinkOptionsTests(unittest.TestCase):
    def test_entire_pinned_gn_flag_sets_are_classified(self):
        for platform in ('linux', 'windows'):
            with self.subTest(platform=platform):
                flags = json.loads((HERE/'fixtures'/(platform+'-link-options.json')).read_text())['ldflags']
                kept, omitted = exporter.link_options(flags, platform=='windows', lambda p: p)
                self.assertEqual(set(flags), set(kept) | {x['flag'] for x in omitted})
                if platform=='windows':
                    self.assertIn('/FIXED:NO', kept)
                    self.assertIn('/guard:cf', kept)
                    self.assertIn('synchronization.lib', kept)
                    self.assertIn('legacy_stdio_definitions.lib', kept)
                    self.assertNotIn('--color-diagnostics', kept)
    def test_unreviewed_libraries_and_dynamic_engine_still_fail(self):
        for flag in ('unknown.lib', '../../unreviewed.lib', 'dxcompiler.lib',
                     '/DEFAULTLIB:dxcompiler.dll.lib', '/DELAYLOAD:dxil.dll',
                     '/DELAYLOAD:libcef.dll', '/UNREVIEWED', '/FIXED'):
            with self.subTest(flag=flag), self.assertRaises(RuntimeError):
                exporter.link_options([flag], True, lambda p: p)

if __name__=='__main__': unittest.main()
