#!/usr/bin/env python3
"""Actual GN/Ninja target-prefix regression, never a Chromium/CEF build claim."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile
import gn_platform as gn_bridge
import platform_contract as contract

GN_URL = 'https://chrome-infra-packages.appspot.com/dl/gn/gn/linux-amd64/+/fj2NZKMkIYZNH6uYG0bn8OsW_lZB5JKz3JeScMCLAGQC'
GN_SHA512 = 'd49575bd383b6aace1257a6e9439ce0a206173ec2cab94d5312f06db412e09c89aa75b1f4c69f5dca4389d15a489c211a73439a66f437c34b18bc90eefa0b775'


def acquire(evidence: Path) -> tuple[Path, dict[str, bytes]]:
    archive = evidence/'gn-linux-amd64.zip'
    if not archive.exists():
        with urllib.request.urlopen(GN_URL, timeout=120) as response:
            data=response.read(16*1024**2+1)
        contract.require(len(data)<=16*1024**2, 'Oversized GN archive')
        archive.write_bytes(data)
    contract.require(hashlib.sha512(archive.read_bytes()).hexdigest()==GN_SHA512,'GN archive hash mismatch')
    with zipfile.ZipFile(archive) as z:
        data=z.read('gn')
    gn=evidence/'gn';gn.write_bytes(data);gn.chmod(0o755)
    originals={}
    for path, blob in {gn_bridge.PKG_CONFIG_PATH:gn_bridge.PKG_CONFIG_BLOB, **gn_bridge.DIRECT_BLOBS}.items():
        local=evidence/('upstream-'+path.replace('/','-'))
        if not local.exists():
            url=f'https://raw.githubusercontent.com/chromium/chromium/{gn_bridge.CHROMIUM}/{path}'
            with urllib.request.urlopen(url,timeout=60) as response: data=response.read(1024**2+1)
            contract.require(len(data)<=1024**2,'Oversized GN source')
            local.write_bytes(data)
        data=local.read_bytes()
        contract.require(gn_bridge.git_blob(data)==blob,'Pinned GN source bytes changed: '+path)
        originals[path]=data
    return gn, originals


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True)
    args=p.parse_args();work=args.work.resolve();evidence=args.evidence.resolve()
    contract.require(not work.exists() and not evidence.is_relative_to(work),'Fresh work and separate evidence required')
    work.mkdir(parents=True);evidence.mkdir(parents=True,exist_ok=True)
    gn,originals=acquire(evidence)
    count=0
    def run(command,cwd=work,*,ok=True):
        nonlocal count
        count+=1
        result=subprocess.run(list(map(str,command)),cwd=cwd,capture_output=True,text=True,timeout=120)
        text=result.stdout+result.stderr
        (evidence/f'{count:02}-native.log').write_text(subprocess.list2cmdline(list(map(str,command)))+'\n'+text)
        print(text,flush=True)
        if ok: result.check_returncode()
        else: contract.require(result.returncode!=0,'Unexpected success of negative GN case')
        return result.stdout
    prefix=work/'target prefix'
    for folder in ('include','lib/pkgconfig'):(prefix/folder).mkdir(parents=True)
    (prefix/'include/value.h').write_text('int value(void);\n')
    for name,code in [('a','int from_b(void); int value(void){return from_b();}'),
                      ('b','int from_a(void); int from_b(void){return from_a();}'),
                      ('a2','int from_a(void){return 42;}')]:
        (work/(name+'.c')).write_text(code)
        run(['cc','-fPIC','-c',name+'.c','-o',name+'.o'])
    run(['ar','rcs',prefix/'lib/liba.a','a.o','a2.o']);run(['ar','rcs',prefix/'lib/libb.a','b.o'])
    (prefix/'lib/pkgconfig/fixture.pc').write_text(
        'prefix=${pcfiledir}/../..\nlibdir=${prefix}/lib\nincludedir=${prefix}/include\n'
        'Name: fixture\nDescription: native fixture\nVersion: 1.2.3\n'
        'Libs: -L${libdir} -la -lb -pthread\nCflags: -I${includedir} -DFIXTURE=1\n')
    value=contract.capture(prefix,Path(shutil.which('pkg-config')),['fixture'])
    manifest=work/'platform.json';manifest.write_bytes(contract.canonical(value));sha=contract.digest(manifest)
    source=work/'source';source.mkdir()
    def write(name,text):
        path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    # Exercise the full guarded binder using a complete synthetic module catalog.
    # Each alias still points to the same real fixture archives, not real CEF libs.
    full=dict(value,modules={m:value['modules']['fixture'] for m in gn_bridge.MODULES})
    full['modules']['fixture']=value['modules']['fixture']
    manifest.write_bytes(contract.canonical(full));sha=contract.digest(manifest)
    for path,data in originals.items(): write(path,data.decode())
    selected=gn_bridge.bind(source,manifest,prefix,sha)
    contract.require(gn_bridge.bind(source,manifest,prefix,sha)==selected,'Non-idempotent GN binding')
    try: gn_bridge.guard(source,None)
    except ValueError: pass
    else: raise ValueError('Engine-only path accepted a bound strict workspace')
    # Minimal GN build rules around the REAL patched Chromium pkg_config.gni.
    # The three direct call-site transforms were hash checked, but this fixture
    # does not evaluate Chromium's entire printing/audio component graph.
    write('.gn', 'buildconfig = "//build/BUILDCONFIG.gn"\n'
          'default_args = {\n  target_os = "linux"\n  target_cpu = "x64"\n}\n')
    write('build/BUILDCONFIG.gn', '''declare_args() {
  use_lld = true
  use_sysroot = false
  use_remoteexec = false
  use_vaapi = false
  use_v4l2_codec = false
}
current_cpu = target_cpu
is_linux = true
host_toolchain = "//toolchain:host"
sysroot = ""
target_sysroot = ""
compute_inputs_for_analyze = false
set_default_toolchain("//toolchain:target")
''')
    for name in ('build/config/compute_inputs_for_analyze.gni','build/config/gclient_args.gni',
                 'build/config/sysroot.gni','build/toolchain/rbe.gni'): write(name,'# Native fixture stub.\n')
    write('build/config/linux/pkg-config.py',
          'import sys,json\nassert sys.argv[1:]==["host-only"]\nprint(json.dumps([[],[],[],[]]))\n')
    cc=shutil.which('cc');ar=shutil.which('ar')
    tools='''
  tool("cc") {
    command = "CCBIN -MMD -MF {{output}}.d {{defines}} {{include_dirs}} {{cflags}} {{cflags_c}} -c {{source}} -o {{output}}"
    depfile = "{{output}}.d"
    depsformat = "gcc"
    outputs = [ "{{source_out_dir}}/{{target_output_name}}.{{source_name_part}}.o" ]
  }
  tool("link") {
    command = "CCBIN -fuse-ld=lld {{ldflags}} -o {{output}} {{inputs}} {{solibs}} {{libs}}"
    outputs = [ "{{root_out_dir}}/{{target_output_name}}{{output_extension}}" ]
    lib_switch = "-l"
    lib_dir_switch = "-L"
  }
  tool("stamp") {
    command = "touch {{output}}"
  }
'''.replace('CCBIN',cc)
    write('toolchain/BUILD.gn','toolchain("target") {\n'+tools+'}\ntoolchain("host") {\n'+tools+'}\n')
    write('BUILD.gn','''import("//build/config/linux/pkg_config.gni")
if (current_toolchain == default_toolchain) {
  pkg_config("fixture") { packages = [ "fixture" ] }
  executable("probe") {
    sources = [ "main.c" ]
    configs = [ ":fixture" ]
  }
  group("all") { deps = [ ":probe", ":host_probe(//toolchain:host)" ] }
} else {
  pkg_config("host_fixture") { packages = [ "host-only" ] }
  executable("host_probe") {
    sources = [ "host.c" ]
    configs = [ ":host_fixture" ]
  }
}
''')
    write('main.c','#include "value.h"\n#ifndef FIXTURE\n#error Missing target definition\n#endif\nint main(void){return value()!=42;}\n')
    write('host.c','int main(void){return 0;}\n')
    out=source/'out';out.mkdir()
    settings='\n'.join(k+' = '+json.dumps(v) for k,v in selected.items())+'\n'
    (out/'args.gn').write_text(settings)
    run([gn,'gen',out,'--fail-on-unused-args'],source)
    graph=json.loads(run([gn,'desc',out,'//:probe','--format=json'],source))['//:probe']
    proof=gn_bridge.audit_graph(graph,source,out,prefix,full)
    expected_archives = [str(prefix/'lib/liba.a'), str(prefix/'lib/libb.a')]
    contract.require(graph['libs'] == expected_archives, 'GN lost absolute archive ordering')
    contract.require(graph.get('lib_dirs', []) == [], 'Unexpected target library search')
    contract.require(graph['ldflags'] == ['-pthread'], 'GN dropped static link flags')
    host_graph=json.loads(run([gn,'desc',out,'//:host_probe(//toolchain:host)','--format=json'],source))
    host_target=next(iter(host_graph.values()))
    contract.require(not host_target.get('libs'), 'Host toolchain consumed target archives')
    run(['ninja','-C',out,'all']);run([out/'probe']);run([out/'host/host_probe'])
    obj=out/'obj/probe.main.o'
    unchanged=(obj.stat().st_mtime_ns,contract.digest(obj))
    run(['ninja','-C',out,'all'])
    contract.require(unchanged==(obj.stat().st_mtime_ns,contract.digest(obj)),
                     'Unchanged GN/Ninja iteration rebuilt a completed object')
    # GN's regeneration dependencies bind the frozen header bytes.
    (prefix/'include/value.h').write_text('int other(void);\n')
    run(['ninja','-C',out,'all'],source,ok=False)
    proof.update(native_fixture=True,host_toolchain_isolated=True,cycle_link_executed=True,
                 changed_header_rejected=True,unchanged_object_reused=True,chromium_build_verified=False,
                 gn_sha256=contract.digest(gn),gn_version=run([gn,'--version']).strip())
    (evidence/'native-platform.json').write_bytes(contract.canonical(proof))
    print('GN_STATIC_TARGET_PREFIX_FIXTURE_VERIFIED; NOT_A_CEF_BUILD',flush=True)

if __name__=='__main__': main()
