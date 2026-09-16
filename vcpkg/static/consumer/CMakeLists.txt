cmake_minimum_required(VERSION 3.24)
project(cef_static_sdk_consumer LANGUAGES C)
if(NOT DEFINED CEF_STATIC_SMOKE_SOURCE OR CEF_STATIC_SMOKE_SOURCE STREQUAL "")
    message(FATAL_ERROR "Supply CEF_STATIC_SMOKE_SOURCE from the pinned source fixture")
endif()
# Untyped -D values preserve native Windows backslashes. add_executable may
# interpret those again as escapes (for example D:\a). Convert a single native
# path, not a search-path list, before handing it to the source-file machinery.
if(CEF_STATIC_SMOKE_SOURCE MATCHES "[;\r\n]")
    message(FATAL_ERROR "CEF_STATIC_SMOKE_SOURCE must be one literal file path")
endif()
cmake_path(SET _cef_static_smoke_source "${CEF_STATIC_SMOKE_SOURCE}")
cmake_path(IS_ABSOLUTE _cef_static_smoke_source _cef_static_smoke_absolute)
if(NOT _cef_static_smoke_absolute OR NOT EXISTS "${_cef_static_smoke_source}"
        OR IS_DIRECTORY "${_cef_static_smoke_source}")
    message(FATAL_ERROR "CEF_STATIC_SMOKE_SOURCE must name an existing absolute source file")
endif()
find_package(cef-static 152.0.6 CONFIG REQUIRED)
add_executable(cef_static_smoke "${_cef_static_smoke_source}")
set_target_properties(cef_static_smoke PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED ON)
if(MSVC)
    set_property(TARGET cef_static_smoke PROPERTY MSVC_RUNTIME_LIBRARY MultiThreaded)
    # This fixed C-ABI fixture uses portable ISO C stdio, as in the GN build
    # (_CRT_SECURE_NO_DEPRECATE). Keep /WX; do not export this to SDK clients.
    target_compile_definitions(cef_static_smoke PRIVATE _CRT_SECURE_NO_WARNINGS)
    target_compile_options(cef_static_smoke PRIVATE /W4 /WX)
else()
    target_compile_options(cef_static_smoke PRIVATE -Wall -Wextra -Werror)
endif()
target_link_libraries(cef_static_smoke PRIVATE CEF::static)
cef_static_deploy_resources(cef_static_smoke)
