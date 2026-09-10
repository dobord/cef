# vcpkg sanitizes PATH. A depot_tools git.bat is not a native executable for
# Python subprocess(shell=False) on Windows. Acquire and pass Git explicitly.
vcpkg_find_acquire_program(GIT)
get_filename_component(_cef_git_bin "${GIT}" DIRECTORY)
vcpkg_add_to_path(PREPEND "${_cef_git_bin}")
set(ENV{CEF_STATIC_GIT} "${GIT}")
