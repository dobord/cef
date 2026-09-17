"""Platform-aware worker identities and real checkpoint file clocks."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/'vcpkg/integration'))
import driver

@unittest.skipUnless(sys.platform == 'linux', 'Linux target-prefix contract')
class PlatformDriverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.work=self.root/'work'
        self.selected={'manifest':str(self.work/'platform.json'),
                       'prefix':str(self.work/'target-prefix'),'sha256':'a'*64}

    def test_prefix_and_contract_must_both_be_checkpointed(self):
        self.assertEqual(driver.checked_platform(self.work,self.selected),self.selected)
        for key in ('manifest','prefix'):
            bad=dict(self.selected,**{key:str(self.root/'outside')})
            with self.subTest(key=key),self.assertRaises(ValueError):
                driver.checked_platform(self.work,bad)
        bad=dict(self.selected,manifest=self.selected['prefix']+'/input.json')
        with self.assertRaises(ValueError):driver.checked_platform(self.work,bad)

    def test_symlink_cannot_move_prefix_outside_workspace(self):
        self.work.mkdir();outside=self.root/'outside';outside.mkdir()
        Path(self.selected['prefix']).symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):driver.checked_platform(self.work,self.selected)

    def test_semantic_identity_tracks_graph_and_helper_implementation(self):
        with patch.object(driver.adapter(),'ci_identity',return_value={'recipe':'recipe'}):
            legacy=driver.identity(self.work,'b'*64)
            native=driver.identity(self.work,'b'*64,self.selected)
            self.assertEqual(legacy,{'recipe':'recipe','build_contract':'b'*64,'worker_schema':1})
            self.assertEqual(native['worker_schema'],2)
            for field in ('manifest','prefix','sha256'):
                other=dict(self.selected,**{field:('c'*64 if field=='sha256' else str(self.work/('different-'+field)))})
                self.assertNotEqual(native,driver.identity(self.work,'b'*64,other))
            with patch.object(driver.build,'digest',return_value='0'*64):
                self.assertNotEqual(native,driver.identity(self.work,'b'*64,self.selected))

    def test_progress_uses_the_selected_output_directory_only(self):
        out=self.work/'download/chromium/src/out/CEF_Static_Platform_Release_x64'
        out.mkdir(parents=True);(out/'one.o').write_bytes(b'object')
        (out/'.ninja_log').write_text('# ninja log v5\n0\t1\t1\tone.o\thash\n')
        self.assertEqual(driver.ninja_state(self.work),{})
        self.assertEqual(set(driver.ninja_state(self.work,self.selected)),{'one.o'})

    def test_real_checkpoint_preserves_dependency_header_and_archive_clocks(self):
        prefix=Path(self.selected['prefix']);(prefix/'include').mkdir(parents=True)
        (prefix/'lib').mkdir()
        header=prefix/'include/value.h';header.write_bytes(b'int value(void);\n')
        library=prefix/'lib/libfixture.a';library.write_bytes(b'fixture bytes; not a real engine archive')
        Path(self.selected['manifest']).write_text('{}')
        for path in (header,library):os.utime(path,ns=(1234567890123456000,1234567890123456000))
        before={p.relative_to(self.work).as_posix():(p.read_bytes(),p.stat().st_mtime_ns) for p in (header,library)}
        with patch.object(driver.adapter(),'ci_identity',return_value={'recipe':'fixture'}):
            ident=driver.identity(self.work,'b'*64,self.selected)
        package=self.root/'checkpoint';driver.adapter().save(self.work,package,ident,limit=256)
        self.work.rename(self.root/'hidden')
        driver.adapter().restore(package,self.work,ident)
        for name,expected in before.items():
            path=self.work/name;self.assertEqual((path.read_bytes(),path.stat().st_mtime_ns),expected)
        bad=dict(ident,platform_inputs=dict(self.selected,sha256='c'*64))
        with self.assertRaises(ValueError):driver.adapter().restore(package,self.root/'bad',bad)

if __name__=='__main__':unittest.main()
