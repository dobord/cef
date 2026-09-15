#!/usr/bin/env python3
"""Download exact pinned RNG files and apply the pinned upstream CEF patch.

This prepares full-source unit fixtures, not a Chromium build or runtime proof.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CHROMIUM = '79460ebecaa5625e57a5fb679a735659e73dc687'
BEFORE = {'base/rand_util.cc': 'ee86984a222c44d29c83da0134a93b0e7ddaa1e4',
          'base/rand_util.h': 'a9ca1f92958e792db46c3e11182b6313a63ae1bc'}
AFTER = {'base/rand_util.cc': '5580637cc84be708c8fe65b535fae219555515c3',
         'base/rand_util.h': 'd14daa58e41ccbfc6638a43b10d0c9858ef8f0cf'}
PATCH = 'f9174db23a6a1a0ba231d0f3009dac2cd8e7a32f'


def blob(data: bytes) -> str:
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def prepare(destination: Path) -> None:
    if destination.exists():
        raise RuntimeError('Runtime fixture destination must not already exist')
    patch = ROOT/'patch/patches/base_rand_util_lazy_seed.patch'
    data = patch.read_bytes().replace(b'\r\n',b'\n')
    if blob(data) != PATCH:
        raise RuntimeError('Not the pinned upstream CEF random patch')
    destination.mkdir(parents=True)
    local_patch = destination/'cef-rand.patch';local_patch.write_bytes(data)
    for name, expected in BEFORE.items():
        url=f'https://raw.githubusercontent.com/chromium/chromium/{CHROMIUM}/{name}'
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=60) as response:
                    data=response.read()
                break
            except OSError:
                if attempt==2:raise
                time.sleep(3)
        if blob(data)!=expected:raise RuntimeError('Pinned runtime input mismatch: '+name)
        file=destination/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(data)
    git=shutil.which('git.exe' if os.name=='nt' else 'git')
    if not git:raise RuntimeError('Native Git required to prepare exact runtime fixture')
    subprocess.run([git,'init','-q',destination],check=True,timeout=30)
    subprocess.run([git,'-C',destination,'config','core.autocrlf','false'],check=True,timeout=30)
    for flags in (['--check'],[]):
        subprocess.run([git,'-C',destination,'apply','-p0',*flags,local_patch],check=True,timeout=30)
    for name, expected in AFTER.items():
        if blob((destination/name).read_bytes())!=expected:
            raise RuntimeError('CEF runtime patch output mismatch: '+name)
    (destination/'provenance.json').write_text(json.dumps({'chromium_commit':CHROMIUM,
        'upstream_cef_patch_blob':PATCH,'before_blobs':BEFORE,'after_blobs':AFTER,
        'fixture_only':True,'engine_runtime_verified':False},indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args();prepare(args.destination.resolve())
