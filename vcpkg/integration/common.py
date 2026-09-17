"""Versioned, bounded input validation for the CEF package adapter (stdlib only)."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

SCHEMA = 1
PLATFORMS = {"linux": "x64-linux-static-release", "windows": "x64-windows-static-release"}
SOURCE_TRIPLETS = {"linux": "x64-linux", "windows": "x64-windows-static"}
PROFILES = frozenset({"engine-static", "static-third-party"})
HEX = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


def unique(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result, "duplicate JSON field")
        result[name] = value
    return result


def read_json(path: Path, maximum: int = 32 * 1024**2):
    require(regular(path) and path.stat().st_size <= maximum, "missing, redirected or oversized JSON")
    return json.loads(path.read_bytes(), object_pairs_hook=unique,
                      parse_constant=lambda _: require(False, "nonfinite JSON"))


def regular(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and not path.is_symlink() and not getattr(info, "st_file_attributes", 0) & 0x400


def digest(path: Path, algorithm: str = "sha256") -> str:
    require(regular(path), "expected a regular file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def write_json(path: Path, value) -> None:
    """A receipt appears atomically; never leave a partial success marker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def relative(name: str) -> PurePosixPath:
    require(isinstance(name, str) and 0 < len(name) <= 4096, "invalid path")
    require(not any(ord(c) < 32 or ord(c) == 127 for c in name), "control character in path")
    require(not any(c in name for c in '\\:\"<>|?*'), "nonportable path")
    pieces = name.split("/")
    require(len(pieces) <= 64 and all(p not in ("", ".", "..") and not p.endswith((" ", ".")) for p in pieces), "unsafe path")
    require(not any(re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p, re.I) for p in pieces), "reserved path")
    require(not any(p.casefold() == ".git" for p in pieces), "Git metadata in SDK")
    return PurePosixPath(name)


def validate_lock(lock: dict) -> dict:
    require(isinstance(lock, dict) and set(lock) == {"schema", "recipe_sha", "cef_sha", "chromium_sha", "acquisition", "platforms"}, "invalid CEF lock fields")
    require(type(lock["schema"]) is int and lock["schema"] == SCHEMA, "unsupported CEF lock schema")
    for key in ("recipe_sha", "cef_sha", "chromium_sha"):
        require(isinstance(lock[key], str) and COMMIT.fullmatch(lock[key]) is not None, "invalid pinned revision")
    require(lock["acquisition"] in {"source", "release"}, "invalid acquisition; no automatic fallback")
    require(isinstance(lock["platforms"], dict) and set(lock["platforms"]) == set(PLATFORMS), "both platforms are required")
    for platform, cfg in lock["platforms"].items():
        fields = {"profile", "triplet"} | ({"release"} if lock["acquisition"] == "release" else set())
        require(isinstance(cfg, dict) and set(cfg) == fields, "invalid platform lock")
        require(cfg["triplet"] == PLATFORMS[platform] and cfg["profile"] in PROFILES, "unsupported triplet or profile")
        if lock["acquisition"] == "release":
            release = cfg["release"]
            require(isinstance(release, dict) and set(release) == {"tag", "commit", "name", "manifest_sha256", "assets"}, "invalid release pin")
            require(isinstance(release["tag"], str) and re.fullmatch(r"cef-[A-Za-z0-9._+-]{1,150}", release["tag"]) is not None, "invalid release tag")
            require(isinstance(release["commit"], str) and COMMIT.fullmatch(release["commit"]) is not None, "invalid tested producer revision")
            require(isinstance(release["name"], str) and re.fullmatch(r"cef-[A-Za-z0-9._+-]{1,170}", release["name"]) is not None, "invalid bundle name")
            require(isinstance(release["manifest_sha256"], str) and HEX.fullmatch(release["manifest_sha256"]) is not None, "manifest pin required")
            require(isinstance(release["assets"], list) and 5 <= len(release["assets"]) <= 132, "invalid asset inventory")
            seen = set()
            for asset in release["assets"]:
                require(isinstance(asset, dict) and set(asset) == {"name", "size", "sha256"}, "invalid asset")
                name = asset["name"]
                require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,190}", name) is not None and ".." not in name, "unsafe asset name")
                require(name not in seen and name.startswith(release["name"]), "ambiguous release asset")
                seen.add(name)
                require(type(asset["size"]) is int and 0 < asset["size"] < 2 * 1024**3, "asset size limit")
                require(isinstance(asset["sha256"], str) and HEX.fullmatch(asset["sha256"]) is not None, "asset hash required")
            manifests = [a for a in release["assets"] if a["name"] == release["name"] + ".manifest.json"]
            require(len(manifests) == 1 and manifests[0]["sha256"] == release["manifest_sha256"], "manifest pin disagreement")
    return lock


def semantic_key(lock: dict, platform: str) -> str:
    """No run, attempt, absolute cache path or slice budget enters package identity."""
    validate_lock(lock)
    require(platform in PLATFORMS, "unsupported platform")
    return hashlib.sha256(canonical({k: v for k, v in lock.items() if k != "platforms"} | {"platform": platform, "target": lock["platforms"][platform]})).hexdigest()
