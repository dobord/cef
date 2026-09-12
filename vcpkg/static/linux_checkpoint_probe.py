#!/usr/bin/env python3
"""Two-runner Linux C/Ninja test through the production POSIX archive adapter."""
import json
import os
from pathlib import Path
import sys
import linux_checkpoint as cp
import checkpoint_probe as native


def main():
    cp.require_linux()
    # Explicitly reuse the existing native fixture while exercising the Linux
    # writer/reader used in production, not the Windows collision policy.
    native.cp = cp
    native.main()
    package = Path('linux-posix-fixture').resolve()
    work = Path(os.environ['RUNNER_TEMP'])/'cef-linux-posix-fixture'
    identity = {'platform': 'linux-x64', 'recipe': 'posix-metadata-fixture-v1'}
    if sys.argv[1] == 'produce':
        work.mkdir()
        for name, value in [('Case', 'upper'), ('case', 'lower')]:
            (work/name).mkdir()
            (work/name/'payload').write_text(value)
        tool = work/'tool.sh'
        tool.write_text('#!/bin/sh\nprintf "POSIX_OK\\n"\n')
        tool.chmod(0o755)
        os.link(tool, work/'tool-hardlink.sh')
        os.symlink('tool.sh', work/'tool-link.sh')
        cp.save(work, package, identity, limit=128)
    else:
        cp.restore(package, work, identity)
        if (work/'Case/payload').read_text() != 'upper' or (work/'case/payload').read_text() != 'lower':
            raise RuntimeError('Linux case-sensitive paths were lost')
        for name in ('tool.sh', 'tool-hardlink.sh', 'tool-link.sh'):
            native.run([work/name])
        proof = Path('checkpoint-probe-result.json')
        result = json.loads(proof.read_text())
        result.update(linux_archive_adapter=True, case_sensitive_paths=True,
                      executable_modes=True, hardlink_contents=True)
        proof.write_text(json.dumps(result, indent=2)+'\n')


if __name__ == '__main__':
    main()
