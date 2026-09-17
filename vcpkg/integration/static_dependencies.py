"""Replace the exporter's system -l entries with a closed vcpkg static interface.

The Chromium-owned archive closure is untouched. External libraries remain owned
by their vcpkg ports and are resolved ONLY inside the selected target prefix.
"""
from __future__ import annotations
import os
from pathlib import Path
import re
import shlex
import subprocess
from common import require, regular, relative, digest, read_json, write_json

# Each name was observed in the pinned native link graph. Unknown names require
# an explicit review instead of an accidental link to the build machine's .so.
MODULES = {
    "glib-2.0": "glib-2.0", "gobject-2.0": "gobject-2.0", "gmodule-2.0": "gmodule-2.0",
    "gthread-2.0": "gthread-2.0", "gio-2.0": "gio-2.0",
    "nspr4": "nspr", "plc4": "nspr", "plds4": "nspr",
    "nss3": "nss", "nssutil3": "nss", "smime3": "nss", "ssl3": "nss",
    "atk-1.0": "atk", "atk-bridge-2.0": "atk-bridge-2.0", "atspi": "atspi-2",
    "dbus-1": "dbus-1", "cups": "cups",
    "X11": "x11", "Xcomposite": "xcomposite", "Xdamage": "xdamage", "Xext": "xext",
    "Xfixes": "xfixes", "Xrandr": "xrandr", "Xrender": "xrender", "Xtst": "xtst", "Xi": "xi",
    "xcb": "xcb", "xkbcommon": "xkbcommon", "gbm": "gbm", "drm": "libdrm",
    "expat": "expat", "uuid": "uuid", "pci": "libpci", "udev": "libudev",
    "cairo": "cairo", "harfbuzz": "harfbuzz", "pango-1.0": "pango", "pangocairo-1.0": "pangocairo",
    "asound": "alsa", "z": "zlib",
}
OS_LIBRARIES = frozenset({"c", "m", "dl", "pthread", "rt", "resolv"})


def pkg_environment(prefix: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("PKG_CONFIG_PATH", "PKG_CONFIG_SYSROOT_DIR", "PKG_CONFIG_TOP_BUILD_DIR", "PKG_CONFIG_SYSTEM_LIBRARY_PATH", "PKG_CONFIG_SYSTEM_INCLUDE_PATH"):
        env.pop(key, None)
    env["PKG_CONFIG_LIBDIR"] = os.pathsep.join(str(prefix / p) for p in ("lib/pkgconfig", "share/pkgconfig"))
    env["PKG_CONFIG_ALLOW_SYSTEM_LIBS"] = "1"
    return env


def cmake_quote(value: str) -> str:
    require(not any(c in value for c in (';', '\n', '\r', '\x00', '"')), "unsafe CMake value")
    return '"' + value.replace('\\', '/') + '"'


def resolve_flags(flags: list[str], prefix: Path) -> tuple[list[str], list[str], list[str]]:
    """No default system search. Retain ordering; group archives for cycles."""
    prefix = prefix.resolve()
    directories = [prefix / "lib"]
    libraries, options, os_libraries = [], [], []
    for flag in flags:
        require(isinstance(flag, str) and not any(c in flag for c in (";", "\n", "\r", "\x00")), "unsafe pkg-config output")
        if flag.startswith("-L"):
            directory = Path(flag[2:]).resolve()
            require(directory.is_dir() and directory.is_relative_to(prefix / "lib"), "pkg-config escaped target library prefix")
            if directory not in directories:
                directories.append(directory)
    for flag in flags:
        if flag.startswith("-L"):
            continue
        if flag in ("-pthread", "-Wl,--export-dynamic"):
            options.append(flag)
            continue
        if flag.startswith("-l"):
            name = flag[2:]
            require(re.fullmatch(r"[A-Za-z0-9_.+-]+", name) is not None, "unsupported library syntax")
            if name in OS_LIBRARIES:
                os_libraries.append(name)
                continue
            candidates = [p / ("lib" + name + ".a") for p in directories if (p / ("lib" + name + ".a")).exists()]
            require(len(candidates) == 1, "missing or ambiguous static archive: " + name)
            path = candidates[0]
        elif os.path.isabs(flag):
            path = Path(flag)
        else:
            raise ValueError("unreviewed pkg-config link option: " + flag)
        require(regular(path) and path.resolve().is_relative_to(prefix / "lib") and path.suffix == ".a", "external/shared library in static closure")
        with path.open("rb") as stream:
            require(stream.read(8) == b"!<arch>\n", "thin or invalid external archive")
        libraries.append(path.relative_to(prefix).as_posix())
    return list(dict.fromkeys(libraries)), list(dict.fromkeys(options)), list(dict.fromkeys(os_libraries))


def resolve(system_libraries: list[str], prefix: Path, pkgconf: Path) -> dict:
    modules, os_libraries = [], []
    require(regular(pkgconf), "native pkg-config executable required")
    for library in system_libraries:
        if library in OS_LIBRARIES:
            os_libraries.append(library)
        else:
            require(library in MODULES, "unknown external CEF library: " + library)
            modules.append(MODULES[library])
    modules = list(dict.fromkeys(modules))
    require(modules, "empty external dependency contract")
    result = subprocess.run([str(pkgconf), "--static", "--libs", *modules], env=pkg_environment(prefix),
                            capture_output=True, text=True, check=True, timeout=120)
    require(len(result.stdout) < 1024 * 1024, "oversized pkg-config result")
    archives, options, extra = resolve_flags(shlex.split(result.stdout), prefix)
    require(archives, "pkg-config returned no static dependencies")
    return {"schema": 1, "profile": "static-third-party", "modules": modules,
            "archives": [{"path": p, "sha256": digest(prefix / p)} for p in archives],
            "link_options": options, "os_libraries": list(dict.fromkeys(os_libraries + extra)),
            "runtime_verified": False}


def platform_cmake(contract: dict) -> str:
    rows = ["# Generated static target closure. No system-library lookup is performed.",
            "if(NOT TARGET CEF::platform)", "  add_library(CEF::platform INTERFACE IMPORTED GLOBAL)"]
    names = []
    for index, entry in enumerate(contract["archives"]):
        relative(entry["path"])
        name = "CEF::platform_archive_" + str(index)
        names.append(name)
        path = "${_cef_static_prefix}/" + entry["path"]
        rows += ["  if(NOT EXISTS " + cmake_quote(path) + ")",
                 '    message(FATAL_ERROR "A required static CEF dependency is absent from the SDK")', "  endif()",
                 "  add_library(" + name + " STATIC IMPORTED GLOBAL)",
                 "  set_property(TARGET " + name + " PROPERTY IMPORTED_LOCATION " + cmake_quote(path) + ")"]
    links = (["$<LINK_GROUP:RESCAN," + ",".join(names) + ">"] if names else []) + contract["os_libraries"]
    rows += ["  set_property(TARGET CEF::platform PROPERTY INTERFACE_LINK_LIBRARIES",
             *["    " + cmake_quote(item) for item in links], "  )",
             "  set_property(TARGET CEF::platform PROPERTY INTERFACE_LINK_OPTIONS",
             *["    " + cmake_quote(item) for item in contract["link_options"]], "  )", "endif()", ""]
    return "\n".join(rows)


def apply(prefix: Path, dependency_prefix: Path, pkgconf: Path) -> dict:
    share = prefix / "share/cef-static"
    require(not (share / "static-platform-inventory.json").exists(), "CEF dependency bridge already applied")
    inventory = read_json(share / "static-link-inventory.json")
    libraries = inventory.get("system_libraries")
    require(isinstance(libraries, list) and all(isinstance(p, str) for p in libraries), "missing native link inventory")
    contract = resolve(libraries, dependency_prefix.resolve(), pkgconf.resolve())
    path = share / "cef-static-config.cmake"
    text = path.read_text(encoding="utf-8")
    for library in dict.fromkeys(libraries):
        line = "  " + cmake_quote(library) + "\n"
        require(text.count(line) == 1, "native CMake interface differs from inventory: " + library)
        text = text.replace(line, "", 1)
    marker = "set_property(TARGET CEF::static PROPERTY INTERFACE_LINK_LIBRARIES\n"
    require(text.count(marker) == 1, "ambiguous CEF link interface")
    text = text.replace(marker, 'include("${CMAKE_CURRENT_LIST_DIR}/cef-platform-static.cmake")\n' + marker, 1)
    # External archives must follow the engine's archive group; otherwise the
    # linker can discard them before encountering the engine's references.
    text += '\nset_property(TARGET CEF::static APPEND PROPERTY INTERFACE_LINK_LIBRARIES CEF::platform)\n'
    text += '\nset(CEF_STATIC_DEPENDENCY_PROFILE "static-third-party")\n'
    (share / "cef-platform-static.cmake").write_text(platform_cmake(contract), encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    write_json(share / "static-platform-inventory.json", contract)
    return contract
