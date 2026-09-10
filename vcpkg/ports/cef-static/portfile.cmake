# Experimental until both native source/SDK-consumer CI jobs have passed.
# This is deliberately a separate port from the hybrid `cef` binary SDK port.
if(NOT VCPKG_TARGET_ARCHITECTURE STREQUAL "x64")
    message(FATAL_ERROR "cef-static requires native x64")
endif()
if(NOT (VCPKG_TARGET_IS_WINDOWS OR VCPKG_TARGET_IS_LINUX))
    message(FATAL_ERROR "cef-static supports Windows and Linux only")
endif()
if(NOT VCPKG_LIBRARY_LINKAGE STREQUAL "static")
    message(FATAL_ERROR "cef-static requires VCPKG_LIBRARY_LINKAGE=static; no shared fallback exists")
endif()
if(VCPKG_TARGET_IS_WINDOWS AND NOT VCPKG_CRT_LINKAGE STREQUAL "static")
    message(FATAL_ERROR "The Windows engine requires the static release CRT (/MT)")
endif()
# vcpkg compares complete host/target triplet strings for CROSSCOMPILING;
# x64-windows -> x64-windows-static only changes linkage, not architecture.
# scripts/ports.cmake exposes HOST_TRIPLET to portfiles, not the
# VCPKG_HOST_TRIPLET variable used by CMake manifest consumers.
if(NOT HOST_TRIPLET MATCHES "^x64-")
    message(FATAL_ERROR "cef-static requires an x64 host")
endif()
if(VCPKG_TARGET_IS_WINDOWS AND NOT CMAKE_HOST_WIN32)
    message(FATAL_ERROR "Windows cef-static must build and execute on Windows")
endif()
if(VCPKG_TARGET_IS_LINUX AND NOT CMAKE_HOST_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR "Linux cef-static must build and execute on Linux")
endif()
# The source recipe currently builds one Release engine configuration. Do not
# pretend a release /MT engine is a separately compiled /MTd engine.
set(VCPKG_BUILD_TYPE release)
vcpkg_find_acquire_program(PYTHON3)
set(_work "${CURRENT_BUILDTREES_DIR}/w")
if(DEFINED ENV{CEF_STATIC_WORK} AND NOT "$ENV{CEF_STATIC_WORK}" STREQUAL "")
    set(_work "$ENV{CEF_STATIC_WORK}")
endif()
set(_logs "${CURRENT_BUILDTREES_DIR}/diagnostics")
file(MAKE_DIRECTORY "${_work}" "${_logs}")
message(STATUS "Building CEF/Chromium from pinned sources. C API only; no libcef DLL/SO is downloaded.")
vcpkg_execute_required_process(
    COMMAND "${PYTHON3}" "${CURRENT_PORT_DIR}/source_build.py" build
        --work "${_work}" --logs "${_logs}" --jobs "${VCPKG_CONCURRENCY}"
    WORKING_DIRECTORY "${CURRENT_BUILDTREES_DIR}"
    LOGNAME static-engine-source-build
)
vcpkg_execute_required_process(
    COMMAND "${PYTHON3}" "${CURRENT_PORT_DIR}/export_static.py"
        --source "${_work}/download/chromium/src"
        --out "${_work}/download/chromium/src/out/CEF_Static_Release_x64"
        --diagnostics "${_logs}" --prefix "${CURRENT_PACKAGES_DIR}"
    WORKING_DIRECTORY "${CURRENT_BUILDTREES_DIR}"
    LOGNAME static-engine-sdk-export
)
file(INSTALL "${CURRENT_PORT_DIR}/usage" DESTINATION "${CURRENT_PACKAGES_DIR}/share/${PORT}")
