import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import iteration

class CheckpointAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.file = self.work / "object.o"
        self.file.write_bytes(b"synthetic object")
        os.utime(self.file, ns=(1700000000123456700, 1700000000123456700))
        self.mtime = self.file.stat().st_mtime_ns
        self.adapter, _ = iteration.modules()
        self.expected = {"schema": 3, "recipe": "a"*64, "work": str(self.work),
                         "repository": "dobord/builder", "ref": "refs/heads/main", "image": "synthetic"}
    def test_roundtrip_preserves_precise_clock(self):
        self.adapter.save(self.work, self.root / "checkpoint", self.expected)
        self.file.unlink(); self.work.rmdir()
        result = iteration.restore(self.root / "checkpoint", self.work, self.expected)
        self.assertFalse(result["engine_runtime_verified"])
        self.assertEqual(self.file.stat().st_mtime_ns, self.mtime)
    def test_legacy_origin_only_is_migratable(self):
        old = dict(self.expected, repository="dobord/cef", ref="refs/heads/static-engine")
        self.adapter.save(self.work, self.root / "checkpoint", old)
        self.file.unlink(); self.work.rmdir()
        with self.assertRaises(ValueError): iteration.restore(self.root / "checkpoint", self.work, self.expected)
        result = iteration.restore(self.root / "checkpoint", self.work, self.expected, {"repository": "dobord/cef", "ref": "refs/heads/static-engine"})
        self.assertEqual(result["input_identity"], old)
    def test_legacy_path_or_recipe_changes_are_refused(self):
        old = dict(self.expected, repository="dobord/cef", ref="refs/heads/static-engine", recipe="b"*64)
        self.adapter.save(self.work, self.root / "checkpoint", old)
        self.file.unlink(); self.work.rmdir()
        with self.assertRaises(ValueError):
            iteration.restore(self.root / "checkpoint", self.work, self.expected, {"repository": "dobord/cef", "ref": "refs/heads/static-engine"})

if __name__ == "__main__": unittest.main()
