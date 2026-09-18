# Shared installer for the external cef-static vcpkg port.
# CEF_RECIPE_SOURCE must be the source pinned by vcpkg_from_git in the port.
if(NOT VCPKG_TARGET_ARCHITECTURE STREQUAL "x64" OR
   NOT TARGET_TRIPLET MATCHES "^x64-(linux|windows)-static-release$")
    message(FATAL_ERROR "CEF requires a native x64 static Release triplet")
endif()
if(NOT VCPKG_LIBRARY_LINKAGE STREQUAL "static" OR NOT VCPKG_BUILD_TYPE STREQUAL "release")
    message(FATAL_ERROR "CEF requires static library linkage and Release-only builds")
endif()
if(VCPKG_TARGET_IS_WINDOWS)
    if(NOT CMAKE_HOST_WIN32 OR NOT VCPKG_CRT_LINKAGE STREQUAL "static")
        message(FATAL_ERROR "Windows CEF requires native Windows and /MT")
    endif()
elseif(VCPKG_TARGET_IS_LINUX)
    if(NOT CMAKE_HOST_SYSTEM_NAME STREQUAL "Linux")
        message(FATAL_ERROR "Linux CEF requires a native Linux host")
    endif()
else()
    message(FATAL_ERROR "Unsupported native CEF platform")
endif()
if(NOT HOST_TRIPLET STREQUAL TARGET_TRIPLET)
    message(FATAL_ERROR "Use the same explicit static-release triplet for host and target")
endif()
if(NOT DEFINED CEF_BUILD_CONTRACT_FILE)
    message(FATAL_ERROR "Missing ABI-tracked CEF build contract")
endif()
file(READ "${CEF_BUILD_CONTRACT_FILE}" _contract)
string(JSON _schema GET "${_contract}" schema)
string(JSON _mode GET "${_contract}" mode)
string(JSON _profile GET "${_contract}" profile)
string(JSON _triplet GET "${_contract}" triplet)
if(NOT _schema EQUAL 1 OR NOT _triplet STREQUAL TARGET_TRIPLET)
    message(FATAL_ERROR "CEF build contract does not match the target triplet")
endif()
if(NOT _profile STREQUAL "engine-static" AND NOT _profile STREQUAL "static-third-party")
    message(FATAL_ERROR "Unsupported CEF linkage profile")
endif()
if(_profile STREQUAL "static-third-party")
    if(NOT "strict-platform" IN_LIST FEATURES)
        message(FATAL_ERROR "static-third-party requires the cef-static[strict-platform] feature")
    endif()
    if(_mode STREQUAL "release-import")
        message(FATAL_ERROR "static-third-party requires a source-built CEF engine; published engine-only SDK archives are not qualified for C++ strict consumers")
    endif()
elseif("strict-platform" IN_LIST FEATURES)
    message(FATAL_ERROR "The strict-platform feature requires the static-third-party build contract")
endif()
vcpkg_find_acquire_program(PYTHON3)
include("${CEF_RECIPE_SOURCE}/vcpkg/ports/cef-static/acquire_git.cmake")
set(_logs "${CURRENT_BUILDTREES_DIR}/diagnostics")
file(MAKE_DIRECTORY "${_logs}")
if(_mode STREQUAL "release-import")
    string(JSON _release_lock GET "${_contract}" release_lock)
    set(_lock_file "${CURRENT_BUILDTREES_DIR}/release.lock.json")
    file(WRITE "${_lock_file}" "${_release_lock}\n")
    vcpkg_execute_required_process(
        COMMAND "${PYTHON3}" "${CEF_RECIPE_SOURCE}/vcpkg/integration/sdk_import.py"
            --lock "${_lock_file}" --triplet "${TARGET_TRIPLET}"
            --cache "${DOWNLOADS}/cef-static-assets" --prefix "${CURRENT_PACKAGES_DIR}"
            --required-profile "${_profile}"
        WORKING_DIRECTORY "${CURRENT_BUILDTREES_DIR}"
        LOGNAME cef-release-import
    )
elseif(_mode STREQUAL "source")
    set(_work "${CURRENT_BUILDTREES_DIR}/cef-work")
    if(DEFINED ENV{CEF_STATIC_WORK} AND NOT "$ENV{CEF_STATIC_WORK}" STREQUAL "")
        set(_work "$ENV{CEF_STATIC_WORK}")
    endif()
    file(MAKE_DIRECTORY "${_work}")
    set(_platform_args)
    set(_platform_export_args)
    set(_cef_out "CEF_Static_Release_x64")
    if(_profile STREQUAL "static-third-party" AND VCPKG_TARGET_IS_LINUX)
        foreach(_name CEF_STATIC_PLATFORM_MANIFEST CEF_STATIC_PLATFORM_PREFIX CEF_STATIC_PLATFORM_SHA256)
            if(NOT DEFINED ENV{${_name}} OR "$ENV{${_name}}" STREQUAL "")
                message(FATAL_ERROR "Linux static-third-party requires ${_name}")
            endif()
        endforeach()
        set(_platform_args
            --platform-manifest "$ENV{CEF_STATIC_PLATFORM_MANIFEST}"
            --platform-prefix "$ENV{CEF_STATIC_PLATFORM_PREFIX}"
            --platform-sha256 "$ENV{CEF_STATIC_PLATFORM_SHA256}")
        set(_platform_export_args ${_platform_args})
        set(_cef_out "CEF_Static_Platform_Release_x64")
    endif()
    vcpkg_execute_required_process(
        COMMAND "${PYTHON3}" "${CEF_RECIPE_SOURCE}/vcpkg/ports/cef-static/source_build.py" build
            --work "${_work}" --logs "${_logs}" --jobs "${VCPKG_CONCURRENCY}"
            ${_platform_args}
        WORKING_DIRECTORY "${CURRENT_BUILDTREES_DIR}"
        LOGNAME cef-native-source-verify
    )
    vcpkg_execute_required_process(
        COMMAND "${PYTHON3}" "${CEF_RECIPE_SOURCE}/vcpkg/ports/cef-static/export_static.py"
            --source "${_work}/download/chromium/src"
            --out "${_work}/download/chromium/src/out/${_cef_out}"
            --diagnostics "${_logs}" --prefix "${CURRENT_PACKAGES_DIR}"
            ${_platform_export_args}
        WORKING_DIRECTORY "${CURRENT_BUILDTREES_DIR}"
        LOGNAME cef-native-export
    )
else()
    message(FATAL_ERROR "Unsupported CEF acquisition mode; no implicit fallback")
endif()

# High-level CEF client headers use a small client-side logging implementation.
# Build it with the vcpkg consumer toolchain instead of importing Chromium's
# C++ runtime. CEF::static remains the C-API engine target; CEF::cpp opts into
# this companion archive and preserves the static engine boundary.
vcpkg_cmake_configure(
    SOURCE_PATH "${CEF_RECIPE_SOURCE}/vcpkg/ports/cef-static/cpp_support"
    OPTIONS
        "-DCEF_RECIPE_SOURCE=${CEF_RECIPE_SOURCE}"
        "-DCEF_PACKAGE_PREFIX=${CURRENT_PACKAGES_DIR}"
)
vcpkg_cmake_install()
if(VCPKG_TARGET_IS_WINDOWS)
    set(_cef_cpp_archive "cef_cpp_support.lib")
else()
    set(_cef_cpp_archive "libcef_cpp_support.a")
endif()
if(NOT EXISTS "${CURRENT_PACKAGES_DIR}/lib/cef-static/${_cef_cpp_archive}")
    message(FATAL_ERROR "CEF C++ client support archive was not installed")
endif()
set(_cef_config "${CURRENT_PACKAGES_DIR}/share/cef-static/cef-static-config.cmake")
if(NOT EXISTS "${_cef_config}")
    message(FATAL_ERROR "CEF static CMake config is missing")
endif()
file(APPEND "${_cef_config}" "
# vcpkg-built C++ client support. The engine target itself remains CAPI-only.
if(NOT TARGET CEF::cpp-support)
  add_library(CEF::cpp-support STATIC IMPORTED)
  get_filename_component(_cef_cpp_prefix \"\${CMAKE_CURRENT_LIST_DIR}/../..\" ABSOLUTE)
  set_target_properties(CEF::cpp-support PROPERTIES
    IMPORTED_LOCATION \"\${_cef_cpp_prefix}/lib/cef-static/${_cef_cpp_archive}\")
endif()
if(NOT TARGET CEF::cpp)
  add_library(CEF::cpp INTERFACE IMPORTED)
  set_property(TARGET CEF::cpp PROPERTY INTERFACE_LINK_LIBRARIES
    \"CEF::cpp-support;CEF::static\")
endif()
set(CEF_STATIC_CPP_API TRUE)
")

file(COPY_FILE "${CEF_BUILD_CONTRACT_FILE}"
    "${CURRENT_PACKAGES_DIR}/share/cef-static/build-contract.json")
file(INSTALL "${CURRENT_PORT_DIR}/usage" DESTINATION "${CURRENT_PACKAGES_DIR}/share/${PORT}")
