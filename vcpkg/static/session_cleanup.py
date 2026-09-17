#!/usr/bin/env python3
"""Strict cleanup of a completed CI invocation, never the Chromium workspace.

Only Windows ERROR_ACCESS_DENIED on an owned read-only regular file gets one
retry. This is not a general permissions repair or a concurrent-attacker-safe
filesystem API. Call after child processes have exited, on the private mkdtemp
session. Failed builds must retain their session and must not call this helper.
"""
from __future__ import annotations
from collections import Counter
import errno
import json
import os
from pathlib import Path
import shutil
import stat

WINDOWS = os.name == 'nt'
READONLY = stat.FILE_ATTRIBUTE_READONLY
REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT


def redirected(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & REPARSE)


def checked_session(session: Path, work: Path) -> Path:
    # Do not resolve the supplied root through a symlink/junction before testing
    # it. A valid session is the direct mkdtemp child, not a matching-name tree
    # elsewhere, the source download, the whole workspace, or a missing root.
    session = Path(os.path.abspath(session))
    info = session.lstat()
    if (not stat.S_ISDIR(info.st_mode) or redirected(info) or
            session.parent != work.resolve(strict=True) or
            not session.name.startswith('cef-sdk-test-') or
            session.name == 'cef-sdk-test-'):
        raise ValueError('Cleanup requires the owned direct cef-sdk-test-* directory')
    return session


def check_readonly_hardlinks(session: Path) -> None:
    """Changing NTFS attributes affects every hard link, including outside links.

    Count names without following links/reparse points before deleting any file.
    All names of a read-only multiply-linked file must belong to this session.
    Writable links need no attribute change and can safely be unlinked normally.
    """
    counts = Counter()
    readonly = {}
    pending = [session]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                # DirEntry.stat reports zero file IDs/link counts on Windows.
                info = os.stat(entry.path, follow_symlinks=False)
                if redirected(info):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    key = (info.st_dev, info.st_ino)
                    counts[key] += 1
                    if getattr(info, 'st_file_attributes', 0) & READONLY:
                        readonly[key] = (info.st_nlink, entry.path)
    for key, (links, path) in readonly.items():
        if not key[1] or counts[key] != links:
            raise ValueError('Read-only hard link is not owned exclusively by session: ' + path)


def retry_readonly(function, path: str, error: BaseException,
                   session: Path, retries: list) -> None:
    if (not WINDOWS or function not in (os.unlink, os.remove) or
            not isinstance(error, PermissionError) or error.errno != errno.EACCES or
            getattr(error, 'winerror', None) != 5):
        raise error
    target = Path(os.path.abspath(path))
    try:
        relative = target.relative_to(session)
    except ValueError:
        raise error
    if not relative.parts:
        raise error
    # Reject the leaf AND every parent that could redirect chmod outside.
    for component in [session, *target.parents[:len(relative.parts)-1], target]:
        if redirected(component.lstat()):
            raise error
    info = target.lstat()
    if (not stat.S_ISREG(info.st_mode) or
            not getattr(info, 'st_file_attributes', 0) & READONLY):
        raise error
    event = {'path': relative.as_posix(), 'winerror': 5,
             'mode_before': stat.S_IMODE(info.st_mode), 'recovered': False}
    retries.append(event)
    os.chmod(target, info.st_mode | stat.S_IWRITE)
    try:
        function(path)  # Exactly once; ACL/lock/I/O errors still fail the job.
    except BaseException as failure:
        try:
            current = target.lstat()
            if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                os.chmod(target, info.st_mode)
        except OSError as restore_error:
            failure.add_note('Could not restore read-only metadata: ' + str(restore_error))
        raise
    event['recovered'] = True


def remove_session(session: Path, work: Path, diagnostics: Path) -> dict:
    """Remove only a successful invocation; persist cleanup independently of SDK proof."""
    report = {'schema': 1, 'status': 'running', 'readonly_retries': []}
    status = diagnostics / 'sdk-cleanup-status.json'

    def write_status():
        status.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')

    write_status()
    try:
        session = checked_session(session, work)
        if WINDOWS:
            check_readonly_hardlinks(session)
        shutil.rmtree(session, onexc=lambda function, path, error:
                      retry_readonly(function, path, error, session, report['readonly_retries']))
        if session.exists():
            raise RuntimeError('Session cleanup did not remove its root')
    except Exception as error:
        report.update(status='failed', error=type(error).__name__ + ': ' + str(error))
        write_status()
        raise
    report['status'] = 'removed'
    write_status()
    return report
