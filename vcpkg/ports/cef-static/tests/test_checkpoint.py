"""Checkpoint tests validate infrastructure, never certify Chromium/CEF."""
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

STATIC = Path(__file__).resolve().parents[3]/'static'
sys.path.insert(0, str(STATIC))
import checkpoint as cp
import windows_slice as sliced

IDENTITY = {'recipe': 'unit-fixture-not-cef', 'work': 'stable-path', 'image': 'test'}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.work = self.root/'workspace'; self.work.mkdir()
        self.package = self.root/'checkpoint'
        self.file = self.work/'out/gen/source with spaces.h'; self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b'test header\n')
        self.time = 1712345678123456700
        os.utime(self.file, ns=(self.time, self.time))
        (self.work/'.ninja_log').write_bytes(b'# ninja log v5\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_split_roundtrip_content_and_exact_times(self):
        manifest = cp.save(self.work, self.package, IDENTITY, limit=43)
        self.assertGreater(len(manifest['parts']), 1)
        shutil.rmtree(self.work)
        cp.restore(self.package, self.work, IDENTITY)
        self.assertEqual(self.file.read_bytes(), b'test header\n')
        self.assertEqual(self.file.stat().st_mtime_ns, self.time)
        self.assertEqual((self.work/'.ninja_log').read_bytes(), b'# ninja log v5\n')
        self.assertFalse(manifest['engine_runtime_verified'])

    def test_changed_recipe_ref_image_or_work_is_rejected(self):
        cp.save(self.work, self.package, IDENTITY)
        shutil.rmtree(self.work)
        for field in ('recipe', 'image', 'work', 'ref'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                cp.restore(self.package, self.work, dict(IDENTITY, **{field: 'different'}))
        self.assertFalse(self.work.exists())

    def test_corrupted_part_rejected_before_extract(self):
        m = cp.save(self.work, self.package, IDENTITY)
        shutil.rmtree(self.work)
        p = self.package/m['parts'][0]['name']; p.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            cp.restore(self.package, self.work, IDENTITY)
        self.assertFalse(self.work.exists())

    def test_existing_work_is_never_overwritten(self):
        cp.save(self.work, self.package, IDENTITY)
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            cp.restore(self.package, self.work, IDENTITY)
        self.assertEqual(self.file.read_bytes(), b'test header\n')

    def test_refuses_recursive_archive(self):
        with self.assertRaises(ValueError):
            cp.save(self.work, self.work/'checkpoint', IDENTITY)

    def test_unsafe_names(self):
        for name in ('../x', '/etc/passwd', 'C:/file', '..\\file', 'a/../../x', 'a//b', './file'):
            with self.subTest(name=name), self.assertRaises(ValueError): cp.relative(name)

    def test_traversal_inside_valid_checksum_archive(self):
        self.package.mkdir()
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            info=tarfile.TarInfo('../escaped'); info.size=1; info.pax_headers['CEF.mtime_ns']=str(self.time)
            archive.addfile(info, io.BytesIO(b'x'))
        part=self.package/'workspace.tar.gz.part0000'; part.write_bytes(data.getvalue())
        manifest={'schema':1,'kind':'build-checkpoint-not-sdk','identity':IDENTITY,
            'engine_runtime_verified':False,'files':1,'unpacked_bytes':1,
            'parts':[{'name':part.name,'bytes':part.stat().st_size,'sha256':cp.digest(part)}]}
        (self.package/'checkpoint.json').write_text(json.dumps(manifest))
        shutil.rmtree(self.work)
        with self.assertRaises(ValueError): cp.restore(self.package,self.work,IDENTITY)
        self.assertFalse((self.root/'escaped').exists())

    def test_credentials_are_not_uploaded(self):
        git=self.work/'.git';git.mkdir();(git/'config').write_text('url = https://secret@github.com/repo\n')
        with self.assertRaisesRegex(ValueError,'Credential'):
            cp.save(self.work,self.package,IDENTITY)
        self.assertFalse((self.package/'checkpoint.json').exists())

    def test_no_reuse_of_existing_snapshot_directory(self):
        self.package.mkdir()
        with self.assertRaises(ValueError): cp.save(self.work,self.package,IDENTITY)

    def test_runtime_success_claim_is_rejected(self):
        cp.save(self.work,self.package,IDENTITY);shutil.rmtree(self.work)
        p=self.package/'checkpoint.json';m=json.loads(p.read_text());m['engine_runtime_verified']=True;p.write_text(json.dumps(m))
        with self.assertRaises(ValueError):cp.restore(self.package,self.work,IDENTITY)


@unittest.skipUnless(shutil.which('ninja'), 'Native Ninja required')
class NinjaStopTests(unittest.TestCase):
    def test_ninja_can_finish_without_any_runtime_certificate(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp);(work/'build.ninja').write_text('build all: phony\ndefault all\n')
            result=sliced.run_ninja(['ninja'],work,work/'log.txt',5)
            self.assertEqual(result['status'],'complete');self.assertFalse(result['engine_runtime_verified'])

    def test_actual_command_error_is_not_a_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp)
            command='"'+sys.executable.replace('\\','/')+'" -c "raise SystemExit(7)"'
            (work/'build.ninja').write_text('rule fail\n  command = '+command+'\nbuild fail: fail\n')
            with self.assertRaises(RuntimeError):sliced.run_ninja(['ninja'],work,work/'log.txt',5)
            self.assertEqual(json.loads((work/'log.json').read_text())['status'],'failed')

    def test_soft_stop_removes_unfinished_output_then_resumes(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp)
            (work/'build.py').write_text("import pathlib,time\np=pathlib.Path('partial.out');p.write_text('unfinished')\n"
                                        "time.sleep(0 if pathlib.Path('resume').exists() else 60)\np.write_text('complete')\n")
            command='"'+sys.executable.replace('\\','/')+'" build.py'
            (work/'build.ninja').write_text('rule slow\n  command = '+command+'\nbuild partial.out: slow build.py\n')
            result=sliced.run_ninja(['ninja'],work,work/'log.txt',1,grace=10)
            self.assertEqual(result['status'],'checkpoint')
            self.assertFalse((work/'partial.out').exists(), 'Ninja must remove incomplete output')
            (work/'resume').touch()
            result=sliced.run_ninja(['ninja'],work,work/'next.txt',5)
            self.assertEqual(result['status'],'complete')
            self.assertEqual((work/'partial.out').read_text(),'complete')


class DiscoveryTests(unittest.TestCase):
    def test_only_previous_attempt_of_same_trusted_workflow_can_restore(self):
        identity=dict(IDENTITY,repository='dobord/cef',ref='refs/heads/static-engine')
        key=cp.artifact_name(identity)
        artifact={'id':5,'name':key+'-22-1','expired':False,
                  'workflow_run':{'id':22,'head_branch':'static-engine'}}
        run={'status':'in_progress','head_repository':{'full_name':'dobord/cef'},'event':'push',
             'path':'.github/workflows/static-engine-build.yml'}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'GITHUB_RUN_ID':'22','GITHUB_RUN_ATTEMPT':'2'}):
            package=Path(temp)/'download';package.mkdir()
            with patch.object(cp,'gh_json',side_effect=[{'artifacts':[artifact]},run]), \
                 patch.object(cp.subprocess,'run') as invoke, patch.object(cp,'restore',return_value={}) as restore:
                result=cp.restore_previous(Path(temp)/'work',package,identity)
                self.assertEqual(result['restored_from_run'],22)
                restore.assert_called_once();invoke.assert_called_once()

    def test_untrusted_workflow_or_current_attempt_skipped(self):
        identity=dict(IDENTITY,repository='dobord/cef',ref='refs/heads/static-engine');key=cp.artifact_name(identity)
        for event in ('pull_request','push'):
            a={'id':5,'name':key+'-10-1','expired':False,'workflow_run':{'id':10,'head_branch':'static-engine'}}
            r={'status':'completed','head_repository':{'full_name':'dobord/cef'},'event':event,
               'path':'.github/workflows/untrusted.yml'}
            with patch.dict(os.environ,{'GITHUB_RUN_ID':'22','GITHUB_RUN_ATTEMPT':'1'}), \
                 patch.object(cp,'gh_json',side_effect=[{'artifacts':[a]},r]), \
                 patch.object(cp.subprocess,'run') as invoke:
                self.assertIsNone(cp.restore_previous(Path('work'),Path('download'),identity));invoke.assert_not_called()

if __name__=='__main__':unittest.main()
