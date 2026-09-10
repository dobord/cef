get_filename_component(_source_port "${CURRENT_PORT_DIR}/../../ports/cef-static" ABSOLUTE)
vcpkg_find_acquire_program(PYTHON3)
include("${_source_port}/acquire_git.cmake")
set(_work "${CURRENT_BUILDTREES_DIR}/work")
set(_logs "${CURRENT_BUILDTREES_DIR}/diagnostics")
file(MAKE_DIRECTORY "${_work}" "${_logs}")
vcpkg_execute_required_process(
    COMMAND "${PYTHON3}" "${_source_port}/source_build.py" tools
        --work "${_work}" --logs "${_logs}"
    WORKING_DIRECTORY "${CURRENT_BUILDTREES_DIR}"
    LOGNAME toolcheck
)
file(INSTALL "${_logs}/toolchain.json" DESTINATION "${CURRENT_PACKAGES_DIR}/share/${PORT}")
file(INSTALL "${_source_port}/../../../LICENSE.txt" DESTINATION "${CURRENT_PACKAGES_DIR}/share/${PORT}" RENAME copyright)
set(VCPKG_POLICY_EMPTY_PACKAGE enabled)
