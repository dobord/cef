"""COFF payload preservation and real thin -> regular archive/link regression.

Native CI sets CEF_ARCHIVE_EXPORT_NATIVE=1 and uses only restored Chromium tools.
The optional local fixture cross-compiles PE on Linux; it is NOT a CEF SDK test.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('archive_export', ROOT/'vcpkg/ports/cef-static/export_static.py')
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


def regular(*payloads):
    data = b'!<arch>\n'
    for i, payload in enumerate(payloads):
        header = f'm{i}.obj/'.encode().ljust(16)+b'0'.ljust(12)+b'0'.ljust(6)+b'0'.ljust(6)+b'100644'.ljust(8)+str(len(payload)).encode().ljust(10)+b'`\n'
        data += header+payload+(b'\n' if len(payload)%2 else b'')
    return data


class ArchiveSafetyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='cef archive safety ')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root/'source'; self.source.mkdir()
        self.thin = self.source/'thin.lib'; self.thin.write_bytes(b'!<thin>\n')
        self.object = self.source/'one.obj'; self.object.write_bytes(b'\x64\x86payload')
        self.dest = self.root/'sdk/output.lib'
        self.logs = self.root/'logs'
        self.ar = self.root/'lld-link.exe'
        self.listing = 'one.obj\n'
        self.payloads = [self.object.read_bytes()]
        self.commands = []

    def invoke(self, args, cwd):
        self.commands.append(args)
        if '/list' in args:
            return self.listing
        self.assertNotIn(self.thin, args)  # never take the broken nested-archive path
        output = Path(next(str(a)[5:] for a in args if str(a).startswith('/OUT:')))
        output.write_bytes(regular(*self.payloads))
        return ''

    def materialize(self):
        with patch.object(export, 'run', side_effect=self.invoke):
            return export.materialize_windows_thin_archive(self.ar, self.thin, self.dest, self.source, self.logs)

    def test_materialized_payloads_and_proof(self):
        result = self.materialize()
        self.assertTrue(result['member_order_and_bytes_verified'])
        self.assertFalse(result['sdk_external_consumer_verified'])
        self.assertEqual(result['member_count'], 1)
        self.assertEqual(result['output_sha256'], export.sha256(self.dest))
        self.assertEqual(export.regular_archive_payloads(self.dest), [(len(self.payloads[0]), hashlib.sha256(self.payloads[0]).hexdigest())])

    def test_absolute_member_within_source_supported(self):
        self.listing = str(self.object)+'\n'
        self.materialize()

    def test_crlf_listing_supported(self):
        self.listing = 'one.obj\r\n'
        self.materialize()

    def test_external_member_is_rejected(self):
        outside = self.root/'external.obj'; outside.write_bytes(b'object')
        self.listing = '../external.obj\n'
        with self.assertRaisesRegex(RuntimeError, 'member'): self.materialize()
        self.assertEqual(len(self.commands), 1)

    def test_missing_member_is_rejected(self):
        self.object.unlink()
        with self.assertRaisesRegex(RuntimeError, 'member'): self.materialize()

    def test_duplicate_resolved_member_rejected(self):
        self.listing = 'one.obj\n./one.obj\n'
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'): self.materialize()

    def test_nested_archive_or_pe_renamed_to_object_rejected(self):
        for payload in [b'!<thin>\n', b'!<arch>\n', b'MZfake']:
            with self.subTest(payload=payload):
                self.object.write_bytes(payload)
                with self.assertRaisesRegex(RuntimeError, 'Nested archive or PE'): self.materialize()

    def test_ambiguous_listing_rejected(self):
        for name in ['one.obj\n\n', '"one.obj"\n', '@members.rsp\n', 'one\robj\n']:
            with self.subTest(name=name):
                self.listing = name
                with self.assertRaises(RuntimeError): self.materialize()

    def test_output_data_loss_and_reordering_rejected(self):
        second = self.source/'two.obj'; second.write_bytes(b'\x64\x86two')
        self.listing = 'one.obj\ntwo.obj\n'
        for payloads in [[self.object.read_bytes()], [self.object.read_bytes(), second.read_bytes()], [b'bad', b'bad']]:
            with self.subTest(payloads=payloads):
                self.payloads = payloads
                with self.assertRaisesRegex(RuntimeError, 'payloads'): self.materialize()
                self.assertFalse(self.dest.exists())
                self.assertFalse((self.logs/'output-materialization.json').exists())

    def test_existing_destination_never_overwritten(self):
        self.dest.parent.mkdir(); self.dest.write_bytes(b'existing')
        with self.assertRaisesRegex(RuntimeError, 'overwrite'): self.materialize()
        self.assertEqual(self.dest.read_bytes(), b'existing')

    def test_failed_librarian_removes_stale_proof(self):
        self.logs.mkdir(); proof = self.logs/'output-materialization.json'; proof.write_text('{}')
        with patch.object(export, 'run', side_effect=RuntimeError('librarian failure')):
            with self.assertRaisesRegex(RuntimeError, 'librarian failure'):
                export.materialize_windows_thin_archive(self.ar,self.thin,self.dest,self.source,self.logs)
        self.assertFalse(proof.exists()); self.assertFalse(self.dest.exists())

    def test_invalid_or_truncated_regular_archive_rejected(self):
        for data in [b'!<thin>\n',b'!<arch>\npartial', regular(b'odd')[:-1], regular(b'12345')[:-3]]:
            self.dest.parent.mkdir(exist_ok=True); self.dest.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                export.regular_archive_payloads(self.dest)

    def test_empty_archive_handled_without_fake_input(self):
        self.listing = ''; self.payloads = []
        result = self.materialize()
        self.assertEqual(result['member_count'], 0)
        self.assertIn('/llvmlibempty', self.commands[-1])


class NativeArchiveTests(unittest.TestCase):
    def tools(self):
        if os.environ.get('CEF_ARCHIVE_EXPORT_NATIVE') == '1':
            work = Path(os.environ.get('CEF_STATIC_WORK') or (Path(os.environ['RUNNER_TEMP'])/'cef-static'))
            tool_dir = work/'download/chromium/src/third_party/llvm-build/Release+Asserts/bin'
            clang, ar = tool_dir/'clang-cl.exe', tool_dir/'lld-link.exe'
            self.assertEqual(os.name, 'nt', 'Pinned native test requires Windows')
            self.assertTrue(clang.is_file() and ar.is_file(), 'Missing pinned restored Chromium tools')
            return clang, ar, True
        clang, ar = shutil.which('clang'), shutil.which('lld-link')
        if not clang or not ar:
            self.skipTest('Optional local COFF fixture requires clang and lld-link; CI requires pinned tools explicitly')
        return Path(clang), Path(ar), False

    def test_large_members_duplicate_basenames_relocate_and_link(self):
        clang, ar, pinned = self.tools()
        output = ROOT/'static-diagnostics/archive-regression'; output.mkdir(parents=True,exist_ok=True)
        (output/'native-proof.json').unlink(missing_ok=True)
        commands = []
        with tempfile.TemporaryDirectory(prefix='CEF archive native spaces ') as tmp:
            root = Path(tmp); source = root/'source'; source.mkdir()
            logs = root/'logs'; logs.mkdir()
            def run(command, cwd=root):
                completed = subprocess.run([str(a) for a in command],cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=90)
                commands.append([str(a) for a in command])
                (output/f'tool-{len(commands):02d}.log').write_text(json.dumps(commands[-1])+'\nexit='+str(completed.returncode)+'\n'+completed.stdout,encoding='utf-8')
                self.assertEqual(completed.returncode,0,completed.stdout[-6000:])
                return completed.stdout
            def compile(name, text):
                path = root/name; path.parent.mkdir(parents=True,exist_ok=True)
                c = path.with_suffix('.c'); c.write_text(text)
                args = [clang, '/nologo', '/c', '/GS-', c, '/Fo'+str(path)] if pinned else [clang,'--target=x86_64-pc-windows-msvc','-c','-fno-stack-protector',c,'-o',path]
                run(args); return path
            first = compile('source/left space/member.obj','int data[262144]={39}; int left(void){return data[0];}\n')
            second = compile('source/right space/member.obj','int right(void){return 3;}\n')
            main = compile('consumer/main.obj','int left(void); int right(void); int mainCRTStartup(void){return left()+right()==42?0:1;}\n')
            self.assertGreater(first.stat().st_size,1024*1024)
            libdir = source/'archives'; libdir.mkdir(); thin = libdir/'engine.lib'
            run([ar,'/lib','/nologo','/llvmlibthin','/out:archives/engine.lib','left space/member.obj','right space/member.obj'],source)
            dest = root/'sdk/regular.lib'
            before = {str(p): (export.sha256(p),p.stat().st_mtime_ns) for p in source.rglob('*') if p.is_file()}
            proof = export.materialize_windows_thin_archive(ar,thin,dest,source,logs)
            self.assertEqual(proof['member_count'],2)
            for path,(digest,mtime) in before.items():
                self.assertEqual(export.sha256(Path(path)),digest); self.assertEqual(Path(path).stat().st_mtime_ns,mtime)
            # Remove the entire original archive/object tree before the link.
            shutil.rmtree(source)
            exe = root/'consumer/consumer.exe'
            run([ar,'/nologo','/entry:mainCRTStartup','/subsystem:console','/nodefaultlib','/machine:x64','/out:'+str(exe),main,dest])
            self.assertEqual(exe.read_bytes()[:2],b'MZ')
            executed = os.name == 'nt'
            if executed: run([exe],exe.parent)
            result = {'fixture_not_cef':True,'pinned_chromium_tools':pinned,
                      'large_member_bytes':first.stat().st_size if first.exists() else proof['members'][0]['size'],
                      'member_count':2,'payload_order_and_bytes_verified':True,
                      'source_unmodified_before_removal':True,'source_removed_before_link':True,
                      'pe_consumer_linked':True,'pe_consumer_executed':executed,
                      'clang':str(clang),'lld_link':str(ar),
                      'clang_sha256':export.sha256(clang),'lld_sha256':export.sha256(ar),
                      'library_sha256':export.sha256(dest),'executable_sha256':export.sha256(exe)}
            output = ROOT/'static-diagnostics/archive-regression'; output.mkdir(parents=True,exist_ok=True)
            (output/'native-proof.json').write_text(json.dumps(result,indent=2)+'\n')
            shutil.copy2(logs/'regular-materialization.json',output/'materialization.json')

if __name__=='__main__': unittest.main()
