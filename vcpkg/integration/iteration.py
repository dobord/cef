"""Adapter around the existing, unchanged CEF checkpoint and Ninja contracts.

Compilation checkpoints are not vcpkg packages. Run/attempt selection and
ciphertext authentication belong to the caller (the trusted builder).
"""
from __future__ import annotations
import argparse
import importlib.util
import os
from pathlib import Path
import sys
from common import require, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


def modules():
    # Only the pinned recipe's module directories are added, never PYTHONPATH.
    sys.path.insert(0, str(ROOT / "static"))
    import checkpoint
    import windows_slice
    if os.name == "nt":
        adapter = checkpoint
    else:
        import linux_checkpoint
        adapter = linux_checkpoint
    return adapter, windows_slice


def identity(work: Path) -> dict:
    adapter, _ = modules()
    return adapter.ci_identity(work.resolve())


def restore(package: Path, work: Path, expected: dict, legacy_origin: dict | None = None) -> dict:
    adapter, _ = modules()
    manifest = read_json(package / "checkpoint.json")
    supplied = manifest.get("identity")
    require(isinstance(supplied, dict), "checkpoint has no identity")
    if legacy_origin is None:
        require(supplied == expected, "checkpoint identity changed")
    else:
        # The caller has authenticated the exact source-repository artifact.
        # ONLY its two origin fields may differ; recipe/path/image/schema stay exact.
        require(legacy_origin == {"repository": "dobord/cef", "ref": "refs/heads/static-engine"}, "unsupported legacy producer scope")
        require(set(supplied) == set(expected), "checkpoint schema changed")
        require(all(supplied[k] == legacy_origin.get(k, expected[k]) for k in expected), "legacy checkpoint needs a reviewed recipe/path/image migration")
    result = adapter.restore(package, work, supplied)
    return {"schema": 1, "status": "restored", "identity": expected,
            "input_identity": supplied, "files": result["files"], "unpacked_bytes": result["unpacked_bytes"],
            "engine_runtime_verified": False}


def ninja_state(work: Path) -> dict[str, str]:
    logfile = work / "download/chromium/src/out/CEF_Static_Release_x64/.ninja_log"
    if not logfile.exists():
        return {}
    rows = logfile.read_text(encoding="utf-8").splitlines()
    require(rows and rows[0].startswith("# ninja log v"), "invalid Ninja log")
    outputs = {}
    for row in rows[1:]:
        fields = row.split("\t")
        require(len(fields) == 5 and fields[3], "malformed Ninja output record")
        # Include the output mtime and command hash so recompilation of existing
        # outputs counts as progress after an explicitly reviewed source update.
        outputs[fields[3]] = fields[2] + ":" + fields[4]
    return outputs


def progress(before: dict, after: dict, complete: bool) -> dict:
    added = set(after) - set(before)
    rebuilt = {p for p in set(after) & set(before) if after[p] != before[p]}
    return {"new_outputs": len(added), "rebuilt_outputs": len(rebuilt),
            "removed_outputs": len(set(before) - set(after)),
            "progress": bool(added or rebuilt or complete), "compilation_complete": complete,
            "engine_runtime_verified": False}


def slice_build(work: Path, diagnostics: Path, checkpoint_dir: Path, expected: dict,
                seconds: int, jobs: int) -> dict:
    require(type(seconds) is int and 1 <= seconds <= 10800 and type(jobs) is int and 1 <= jobs <= 1024, "invalid iteration budget")
    require(expected == identity(work), "iteration environment differs from expected checkpoint identity")
    adapter, slices = modules()
    before = ninja_state(work)
    build = slices.build
    work.mkdir(parents=True, exist_ok=True)
    diagnostics.mkdir(parents=True, exist_ok=True)
    build.setup_environment(work)
    source = build.prepare(work, diagnostics)
    out = build.configuration(source, diagnostics)
    adapter.preflight(work)
    ninja = build.find_binary(source, ["third_party/ninja/ninja.exe"] if os.name == "nt" else ["third_party/ninja/ninja"])
    # On unsafe stop or compiler error, run_ninja raises. No new usable checkpoint
    # is published. The previous immutable checkpoint remains available.
    result = slices.run_ninja([ninja, "-C", out, "-j", str(jobs), "-d", "keeprsp", "cef_static_smoke"],
                              source, diagnostics / "ninja-slice.log", seconds)
    report = progress(before, ninja_state(work), result["status"] == "complete")
    require(report["progress"], "iteration made no measurable progress")
    adapter.save(work, checkpoint_dir, expected)
    report.update(schema=1, status=result["status"], checkpoint_ready=True)
    write_json(diagnostics / "iteration-result.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("identity", "restore", "slice"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--logs", type=Path)
    parser.add_argument("--legacy-cef", action="store_true")
    parser.add_argument("--seconds", type=int, default=9000)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if args.phase == "identity":
        write_json(args.identity, identity(args.work))
        return
    require(args.checkpoint is not None and args.logs is not None, "checkpoint and log paths required")
    expected = read_json(args.identity)
    if args.phase == "restore":
        origin = {"repository": "dobord/cef", "ref": "refs/heads/static-engine"} if args.legacy_cef else None
        write_json(args.logs / "restore-result.json", restore(args.checkpoint, args.work, expected, origin))
    else:
        require(not args.legacy_cef, "legacy selection is only valid on restore")
        slice_build(args.work, args.logs, args.checkpoint, expected, args.seconds, args.jobs)

if __name__ == "__main__": main()
