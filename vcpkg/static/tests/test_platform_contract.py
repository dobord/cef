"""Real ar/pkg-config fixtures; not a CEF runtime qualification."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import platform_contract as pc


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('cc') and shutil.which('ar')
                     and shutil.which('pkg-config'), 'Native Linux C/ar/pkg-config required')
class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='static prefix ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prefix = self.root/'target prefix'
        for folder in ('include', 'lib/pkgconfig', 'share/pkgconfig'):
            (self.prefix/folder).mkdir(parents=True)
        (self.prefix/'include/value.h').write_text('int value(void);\n')
        for name, text in [('a','int from_b(void); int value(void){return from_b();}'),
                           ('b','int from_a(void); int from_b(void){return from_a();}'),
                           ('a2','int from_a(void){return 42;}')]:
            (self.root/(name+'.c')).write_text(text)
            self.run_cmd('cc','-fPIC','-c',name+'.c','-o',name+'.o')
        self.run_cmd('ar','rcs',str(self.prefix/'lib/liba.a'),'a.o','a2.o')
        self.run_cmd('ar','rcs',str(self.prefix/'lib/libb.a'),'b.o')
        self.pc_file('fixture', '-la -lb', '-I${includedir} -DFIXTURE=1')
        self.pkgconf = Path(shutil.which('pkg-config'))
        self.manifest = self.root/'contract.json'

    def run_cmd(self, *args):
        return subprocess.run(args,cwd=self.root,check=True,capture_output=True,text=True,timeout=30)

    def pc_file(self, name, libs, cflags):
        # pkgconf quotes spaces in pcfiledir-derived prefixes itself.
        (self.prefix/f'lib/pkgconfig/{name}.pc').write_text(
            'prefix=${pcfiledir}/../..\nlibdir=${prefix}/lib\nincludedir=${prefix}/include\n'
            f'Name: {name}\nDescription: native fixture\nVersion: 1.2.3\n'
            f'Libs: -L${{libdir}} {libs}\nCflags: {cflags}\n')

    def freeze(self):
        value = pc.capture(self.prefix, self.pkgconf, ['fixture'])
        self.manifest.write_bytes(pc.canonical(value))
        self.sha = pc.digest(self.manifest)
        return value

    def load(self):
        return pc.load(self.manifest,self.sha,self.prefix)

    def test_real_capture_query_and_relocated_inputs(self):
        value = self.freeze()
        self.assertEqual(self.load(),value)
        self.assertEqual(value['modules']['fixture']['libraries'],['lib/liba.a','lib/libb.a'])
        self.assertEqual(value['archive_objects']['lib/liba.a'],2)
        self.assertFalse(value['runtime_verified'])
        result = pc.query(value,self.prefix,['fixture'])
        self.assertEqual(result[2],[str(self.prefix/'lib/liba.a'),str(self.prefix/'lib/libb.a')])
        moved = self.root/'relocated prefix'; self.prefix.rename(moved)
        self.assertEqual(pc.load(self.manifest,self.sha,moved),value)

    def test_manifest_tamper(self):
        self.freeze(); self.manifest.write_bytes(self.manifest.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'contract changed'): self.load()

    def test_same_size_header_change_is_rejected(self):
        self.freeze(); (self.prefix/'include/value.h').write_text('int other(void);\n')
        with self.assertRaisesRegex(ValueError,'bytes changed'): self.load()

    def test_new_shadow_header_is_rejected(self):
        self.freeze(); (self.prefix/'include/shadow.h').write_text('x')
        with self.assertRaisesRegex(ValueError,'Uncaptured target header'): self.load()

    def test_missing_archive_is_not_system_fallback(self):
        (self.prefix/'lib/liba.a').unlink()
        with self.assertRaisesRegex(ValueError,'Missing or ambiguous'): self.freeze()

    def test_polluted_pkg_config_environment_does_not_escape(self):
        poison = self.root/'poison'; poison.mkdir()
        (poison/'fixture.pc').write_text('Name: poison\nDescription: poison\nVersion: 9\nLibs: -levil\n')
        with patch.dict(os.environ,{'PKG_CONFIG_PATH':str(poison),'PKG_CONFIG_SYSROOT_DIR':'/evil',
                                    'PKG_CONFIG_LIBDIR':str(poison)}):
            self.assertEqual(self.freeze()['modules']['fixture']['version'],'1.2.3')

    def test_system_path_and_unreviewed_flags_rejected(self):
        for flag in ('-L/usr/lib','-I/usr/include','-Wl,-rpath,/tmp','-l:liba.a','@response','-includeevil'):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                pc.flags(self.prefix,[flag])

    def test_library_symlink_is_rejected(self):
        path=self.prefix/'lib/liba.a'; target=self.root/'external.a'; path.rename(target); path.symlink_to(target)
        with self.assertRaisesRegex(ValueError,'Symlink'): self.freeze()

    def test_shared_and_thin_archives_rejected(self):
        archive=self.prefix/'lib/liba.a';archive.unlink()
        self.run_cmd('ar','rcsT',str(archive),'a.o')
        with self.assertRaisesRegex(ValueError,'Thin'): self.freeze()
        archive.unlink();self.run_cmd('cc','-shared','a2.o','-o','shared.o')
        self.run_cmd('ar','rcs',str(archive),'shared.o')
        with self.assertRaisesRegex(ValueError,'relocatable'): self.freeze()

    def test_gtk_unix_print_is_header_only_and_manifest_bound(self):
        (self.prefix/'include/gtk-3.0/unix-print/gtk').mkdir(parents=True)
        (self.prefix/'include/gtk-3.0/unix-print/gtk/gtkunixprint.h').write_text(
            'typedef int GtkPrintUnixDialog;\n'
        )
        self.pc_file('gtk+-3.0', '-la -lb', '-I${includedir}')
        self.pc_file(
            'gtk+-unix-print-3.0',
            '',
            '-I${includedir}/gtk-3.0/unix-print',
        )
        value = pc.capture(self.prefix, self.pkgconf, ['fixture', 'gtk+-3.0'])
        self.assertNotIn('gtk+-unix-print-3.0', value['modules'])
        result = pc.query(value, self.prefix, ['gtk+-unix-print-3.0'])
        self.assertEqual(
            result,
            [[str(self.prefix/'include/gtk-3.0/unix-print')], [], [], [], []],
        )
        metadata = [
            name for name in value['files']
            if name.endswith('/gtk+-unix-print-3.0.pc')
        ]
        self.assertEqual(len(metadata), 1)
        header = 'include/gtk-3.0/unix-print/gtk/gtkunixprint.h'
        self.assertIn(header, value['files'])
        value_without_header = copy.deepcopy(value)
        del value_without_header['files'][header]
        with self.assertRaisesRegex(ValueError, 'bytes are not frozen'):
            pc.query(value_without_header, self.prefix, ['gtk+-unix-print-3.0'])
        value_without_pc = copy.deepcopy(value)
        del value_without_pc['files'][metadata[0]]
        with self.assertRaisesRegex(ValueError, 'metadata is not uniquely frozen'):
            pc.query(value_without_pc, self.prefix, ['gtk+-unix-print-3.0'])

    def test_unknown_module_and_filter_cannot_remove_library(self):
        value=self.freeze()
        with self.assertRaisesRegex(ValueError,'uncaptured'):
            pc.query(value,self.prefix,['unseen'])
        with self.assertRaisesRegex(ValueError,'Unreviewed GN static dependency filter'):
            pc.query(value,self.prefix,['fixture'],['liba'])
        with self.assertRaisesRegex(ValueError,'Unreviewed GN static dependency filter'):
            pc.query(value,self.prefix,['fixture'],['FIXTURE'])

    def test_only_reviewed_chromium_filters_remove_static_inputs(self):
        value=self.freeze()
        entry=value['modules']['fixture']
        entry['libraries'] += ['lib/libssl3.a','lib/libfreetype.a']
        entry['includes'] += ['include/freetype2']
        (self.prefix/'include/freetype2').mkdir()
        nss = pc.query(copy.deepcopy(value), self.prefix, ['fixture'], ['-lssl3'])
        self.assertFalse(any(path.endswith('/libssl3.a') for path in nss[2]))
        self.assertTrue(any(path.endswith('/libfreetype.a') for path in nss[2]))
        pango = pc.query(copy.deepcopy(value), self.prefix, ['fixture'], ['freetype'])
        self.assertFalse(any(path.endswith('/libfreetype.a') for path in pango[2]))
        self.assertNotIn(str(self.prefix/'include/freetype2'), pango[0])

    def test_cli_with_gn_order_and_version_modes(self):
        self.freeze()
        base=[sys.executable,pc.__file__,'query','--manifest',str(self.manifest),
              '--prefix',str(self.prefix),'--sha256',self.sha,'fixture']
        result=self.run_cmd(*base)
        self.assertEqual(len(json.loads(result.stdout)),5)
        self.assertEqual(json.loads(self.run_cmd(*base,'--atleast-version','1.2').stdout),True)
        self.assertEqual(json.loads(self.run_cmd(*base,'--atleast-version','2').stdout),False)
        self.assertEqual(json.loads(self.run_cmd(*base,'--version-as-components').stdout),[1,2,3])
        base[2]='inputs';base.pop()
        self.assertIn(str(self.prefix/'include/value.h'),json.loads(self.run_cmd(*base).stdout))

    def test_invalid_metadata(self):
        with self.assertRaises(ValueError): pc.decode(b'{"x":1,"x":2}')
        for name in ('../lib/a.a','/lib/a.a','lib/a;script','lib/./a.a','lib//a.a'):
            with self.subTest(name=name),self.assertRaises(ValueError): pc.member(name)

    @unittest.skipUnless(shutil.which('ld.lld'), 'LLD required for archive cycle test')
    def test_cyclic_native_link_uses_absolute_archives(self):
        value=self.freeze(); (self.root/'main.c').write_text('#include "value.h"\nint main(void){return value()!=42;}')
        includes,flags,libs,dirs,options=pc.query(value,self.prefix,['fixture'])
        self.assertEqual(dirs,[])
        self.run_cmd('cc','main.c',*['-I'+p for p in includes],*flags,*libs,*options,
                     '-fuse-ld=lld','-o','probe')
        self.run_cmd(str(self.root/'probe'))

if __name__=='__main__': unittest.main()
