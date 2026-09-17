import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
STATIC=Path(__file__).resolve().parents[1];sys.path.insert(0,str(STATIC))
import platform_prefix as prefix
import platform_contract as contract

@unittest.skipUnless(sys.platform=='linux' and shutil.which('cc') and shutil.which('ar') and shutil.which('pkg-config'), 'Native Linux static archive tools')
class PrefixTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.installed=self.root/'installed'
        (self.installed/'lib/pkgconfig').mkdir(parents=True);(self.installed/'include').mkdir()
        source=self.root/'source.c';source.write_text('int dependency(void){return 42;}\n')
        subprocess.run(['cc','-c',source,'-o',self.root/'source.o'],check=True)
        subprocess.run(['ar','rcs',self.installed/'lib/libreal.a',self.root/'source.o'],check=True)
        (self.installed/'include/real.h').write_text('int dependency(void);\n')
        (self.installed/'lib/libalias.a').symlink_to('libreal.a')
        (self.installed/'include/alias.h').symlink_to('real.h')
        (self.installed/'lib/pkgconfig/fixture.pc').write_text('prefix=${pcfiledir}/../..\nlibdir=${prefix}/lib\nincludedir=${prefix}/include\nName: fixture\nDescription: actual archive fixture\nVersion: 1.0\nLibs: -L${libdir} -lalias\nCflags: -I${includedir}\n')
        self.dest=self.root/'checkpoint/prefix';self.manifest=self.root/'checkpoint/platform.json'
    def freeze(self):
        return prefix.freeze(self.installed,self.dest,self.manifest,Path(shutil.which('pkg-config')),modules=['fixture'])
    def test_internal_file_aliases_are_materialized_with_exact_bytes_and_clocks(self):
        before=(self.installed/'lib/libreal.a').stat().st_mtime_ns
        result=self.freeze();self.assertEqual(len(result['aliases']),2)
        path=self.dest/'lib/libalias.a';self.assertFalse(path.is_symlink())
        self.assertEqual(path.read_bytes(),(self.installed/'lib/libreal.a').read_bytes())
        self.assertEqual(path.stat().st_mtime_ns,before)
        self.assertTrue((self.installed/'lib/libalias.a').is_symlink())
        contract.load(self.manifest,result['manifest_sha256'],self.dest)
    def test_escape_and_directory_links_fail_without_published_snapshot(self):
        for name,target in [('outside',self.root),('foreign.h',self.root/'source.c')]:
            path=self.installed/'include'/name;path.symlink_to(target)
            with self.assertRaises(ValueError):self.freeze()
            self.assertFalse(self.dest.exists());self.assertFalse(self.manifest.exists());path.unlink()
    def test_shared_object_cannot_hide_behind_a_static_alias(self):
        subprocess.run(['cc','-shared','-fPIC',self.root/'source.c','-o',self.installed/'lib/libreal.a'],check=True)
        with self.assertRaises(ValueError):self.freeze()
        self.assertFalse(self.dest.exists())
    def test_existing_snapshot_not_overwritten(self):
        self.freeze();before=self.manifest.read_bytes()
        with self.assertRaises(ValueError):self.freeze()
        self.assertEqual(self.manifest.read_bytes(),before)
    def test_missing_full_catalog_is_not_promoted(self):
        with self.assertRaises(subprocess.CalledProcessError):
            prefix.freeze(self.installed,self.dest,self.manifest,Path(shutil.which('pkg-config')))
        self.assertFalse(self.dest.exists())
    def test_tools_not_checkpointed_and_manifest_is_relocatable(self):
        (self.installed/'tools').mkdir();(self.installed/'tools/private-tool').write_text('host tool')
        result=self.freeze();self.assertFalse((self.dest/'tools').exists())
        moved=self.root/'moved';self.dest.rename(moved)
        contract.load(self.manifest,result['manifest_sha256'],moved)
        code=self.root/'main.c';code.write_text('#include "alias.h"\nint main(void){return dependency()!=42;}\n')
        subprocess.run(['cc',code,'-I'+str(moved/'include'),moved/'lib/libalias.a','-o',self.root/'app'],check=True)
        subprocess.run([self.root/'app'],check=True)

class HostSeparationTests(unittest.TestCase):
    def test_only_declared_gettext_host_helpers_are_omitted(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'installed';(source/'lib/gettext').mkdir(parents=True)
            (source/'share/gettext').mkdir(parents=True)
            (source/'share/gettext/copyright').write_text('fixture license')
            (source/'lib/gettext/cldr-plurals').write_bytes(b'\x7fELF'+b'not a target library')
            target=root/'snapshot';target.mkdir()
            prefix.copy_payload(source,target)
            self.assertFalse((target/'lib/gettext/cldr-plurals').exists())
            self.assertTrue((source/'lib/gettext/cldr-plurals').exists())
            (source/'lib/gettext/unreviewed-tool').write_bytes(b'\x7fELF'+b'not a target library')
            with self.assertRaisesRegex(ValueError,'Shared/executable'):
                prefix.copy_payload(source,root/'bad')

if __name__=='__main__':unittest.main()
