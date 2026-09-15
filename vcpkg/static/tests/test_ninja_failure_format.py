"""Cover both observed Ninja error formats without accepting signalled edges."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import linux_slice


class FailureFormatTests(unittest.TestCase):
    def test_classic_and_annotated_compiler_errors(self):
        for label in ('obj/bad.o', '[code=1] obj/bad.o', '[code=2] obj/bad.o'):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); out = root/'out'; out.mkdir()
                status = {'status':'failed','unsafe_stop':False,'timed_out':False,'exit_code':1}
                (root/'ninja-slice.json').write_text(json.dumps(status))
                (root/'ninja-slice.log').write_text('FAILED: '+label+' \n'
                    'ninja: build stopped: subcommand failed.\n')
                result = linux_slice.clean_failed_objects(out, root)
                self.assertEqual(result['failed_outputs_removed'], ['obj/bad.o'])
                self.assertEqual(result['status'], 'compile-failed-checkpoint')

    def test_annotation_does_not_weaken_edge_safety(self):
        for label in ('[code=0] obj/bad.o', '[code=130] obj/bad.o', '[code=-9] obj/bad.o',
                      '[code=1] obj/../bad.o', '[code=1] cef_static_smoke',
                      '[code=1] obj/a.o obj/b.o', '[code=1] [code=1] obj/bad.o'):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); out = root/'out'; out.mkdir()
                (root/'ninja-slice.json').write_text(json.dumps(
                    {'status':'failed','unsafe_stop':False,'timed_out':False,'exit_code':1}))
                (root/'ninja-slice.log').write_text('FAILED: '+label+' \n'
                    'ninja: build stopped: subcommand failed.\n')
                with self.assertRaises(RuntimeError):
                    linux_slice.clean_failed_objects(out, root)


if __name__ == '__main__':
    unittest.main()
