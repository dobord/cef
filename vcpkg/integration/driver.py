#!/usr/bin/env python3
"""Reusable native iteration worker. Its caller owns credentials and transport.

No GitHub API calls, dispatch, cache-key decryption or publication happen here.
The existing reviewed source builder, graceful Ninja stop and checkpoint codecs
remain the implementation of the engine. A checkpoint is never an installed SDK.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "vcpkg/static"
sys.path.insert(0, str(STATIC))
import checkpoint as windows_checkpoint
import resume_checkpoint
from windows_slice import build, run_ninja


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def adapter():
    if sys.platform == "linux":
        import linux_checkpoint
        return linux_checkpoint
    if os.name == "nt":
        return windows_checkpoint
    raise ValueError("Only native Windows/Linux x64 workers are supported")


def identity(work: Path, contract: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", contract):
        raise ValueError("A semantic build-contract digest is required")
    result = adapter().ci_identity(work)
    result["build_contract"] = contract
    result["worker_schema"] = 1
    return result


def ninja_state(work: Path) -> dict:
    """Detect both newly added and rebuilt edges, including unchanged filenames."""
    out = work / "download/chromium/src/out/CEF_Static_Release_x64"
    log = out / ".ninja_log"
    if not log.exists():
        return {}
    result = {}
    with log.open(encoding="utf-8") as stream:
        if not re.fullmatch(r"# ninja log v[0-9]+\n?", stream.readline()):
            raise ValueError("Unrecognized Ninja log")
        for row in stream:
            fields = row.rstrip("\n").split("\t")
            if len(fields) != 5 or not fields[3]:
                raise ValueError("Malformed Ninja log")
            name = fields[3]
            path = out / name
            if not path.resolve().is_relative_to(out.resolve()):
                raise ValueError("Ninja output escaped its build directory")
            if path.is_file():
                info = path.stat()
                result[name] = [fields[2], fields[4], info.st_size, info.st_mtime_ns]
    return result


def progress(before: dict, after: dict, complete: bool) -> dict:
    changed = sorted(name for name, value in after.items() if before.get(name) != value)
    return {"status": "progress" if changed or complete else "stalled", "changed_outputs": len(changed),
            "new_outputs": len(after.keys() - before.keys()), "removed_outputs": len(before.keys() - after.keys()),
            "before_outputs": len(before), "after_outputs": len(after), "engine_compilation_complete": complete,
            "engine_runtime_verified": False, "changed_output_sample": changed[:20]}


def slice_build(work: Path, logs: Path, checkpoint: Path, state: Path, contract: str, seconds: int, jobs: int) -> None:
    if not 1 <= seconds <= 10800 or not 1 <= jobs <= 1024:
        raise ValueError("Invalid slice/job budget")
    resume_checkpoint.new_run_only(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    state.unlink(missing_ok=True)
    ident = identity(work, contract)
    work.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    if checkpoint.exists():
        raise ValueError("Refusing to overwrite a checkpoint package")
    build.setup_environment(work)
    source = build.prepare(work, logs)
    out = build.configuration(source, logs)
    before = ninja_state(work)
    write_json(logs / "checkpoint-preflight.json", adapter().preflight(work))
    ninja = build.find_binary(source, ["third_party/ninja/ninja.exe"] if os.name == "nt" else ["third_party/ninja/ninja"])
    failure = None
    try:
        result = run_ninja([ninja, "-C", out, "-j", str(jobs), "-d", "keeprsp", "cef_static_smoke"],
                           source, logs / "ninja-slice.log", seconds)
    except RuntimeError as error:
        if sys.platform != "linux":
            raise
        # Only the existing audited, clean compiler-failure case is reusable.
        # Forced termination, a failed linker and unknown edges remain fatal.
        from linux_slice import clean_failed_objects
        result = clean_failed_objects(out, logs)
        failure = error
    saved = adapter().save(work, checkpoint, ident)
    complete = result["status"] == "complete"
    report = progress(before, ninja_state(work), complete)
    write_json(logs / "progress.json", report)
    write_json(state, {"schema": 1, "kind": "engine-iteration", "status": result["status"],
                      "ready": complete, "checkpoint_ready": True, "engine_runtime_verified": False,
                      "identity": ident, "files": saved["files"], "unpacked_bytes": saved["unpacked_bytes"],
                      "progress": report})
    if failure is not None:
        raise RuntimeError("Compiler failure preserved, not a successful build") from failure
    if report["status"] == "stalled":
        raise RuntimeError("No measurable Ninja progress; refusing another automatic iteration")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("identity", "restore", "slice", "verify-consumer"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--hide", type=Path, action="append", default=[])
    parser.add_argument("--seconds", type=int, default=9000)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    work, logs = args.work.resolve(), args.logs.resolve()
    logs.mkdir(parents=True, exist_ok=True)
    if args.operation == "identity":
        write_json(args.state, identity(work, args.contract))
    elif args.operation == "restore":
        if args.checkpoint is None:
            parser.error("--checkpoint is required")
        restored = adapter().restore(args.checkpoint.resolve(), work, identity(work, args.contract))
        write_json(args.state, {"schema": 1, "kind": "restored-checkpoint", "files": restored["files"],
                               "engine_runtime_verified": False})
    elif args.operation == "slice":
        if args.checkpoint is None:
            parser.error("--checkpoint is required")
        slice_build(work, logs, args.checkpoint.resolve(), args.state, args.contract, args.seconds, args.jobs)
    else:
        if args.executable is None or not args.executable.is_file():
            parser.error("--executable is required")
        args.state.unlink(missing_ok=True)
        executable = args.executable.resolve()
        if os.name == "nt":
            build.audit_windows_binary(ROOT, logs, logs, executable, "consumer-imports")
        else:
            imports = build.run(["readelf", "-d", executable], logs, logs, "consumer-imports")
            build.verify_binary_imports(imports, False)
        hidden = [p.resolve() for p in args.hide]
        if any(executable.is_relative_to(p) or logs.is_relative_to(p) for p in hidden):
            raise ValueError("Runtime evidence and deployed executable must remain outside hidden trees")
        with build.hidden_directories(hidden):
            proof = build.execute_smoke(executable, logs)
        write_json(args.state, {"schema": 1, "kind": "consumer-verification", "engine_linkage": "static",
                               "capi_only": True, "executable_sha256": build.digest(executable), "smoke": proof,
                               "smoke_runs": json.loads((logs / "smoke-runs.json").read_text()),
                               "system_libraries_static": False, "sandbox_verified": False})


if __name__ == "__main__":
    main()
