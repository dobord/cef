cmake_minimum_required(VERSION 3.24)
project(cef_static_cpp_support LANGUAGES CXX)

if(NOT DEFINED CEF_RECIPE_SOURCE OR NOT DEFINED CEF_PACKAGE_PREFIX)
  message(FATAL_ERROR "CEF recipe and package prefix are required")
endif()
cmake_path(CONVERT "${CEF_RECIPE_SOURCE}" TO_CMAKE_PATH_LIST _cef_recipe_source NORMALIZE)
cmake_path(CONVERT "${CEF_PACKAGE_PREFIX}" TO_CMAKE_PATH_LIST _cef_package_prefix NORMALIZE)
if(NOT IS_DIRECTORY "${_cef_recipe_source}")
  message(FATAL_ERROR "CEF_RECIPE_SOURCE must name the pinned CEF recipe")
endif()
if(NOT IS_DIRECTORY "${_cef_package_prefix}/include/cef-static")
  message(FATAL_ERROR "CEF_PACKAGE_PREFIX must contain the exported cef-static headers")
endif()

set(_cef_client_source "${_cef_package_prefix}/share/cef-static/cpp-client")
set(_cef_client_headers "${_cef_package_prefix}/include/cef-static-cpp")
include("${_cef_client_source}/sources.cmake")
add_library(cef_cpp_support STATIC
  ${libcef_dll_wrapper_sources_base}
  ${libcef_dll_wrapper_sources_common}
  ${autogen_client_side})
if(WIN32)
  target_sources(cef_cpp_support PRIVATE ${libcef_dll_wrapper_sources_win})
endif()
target_include_directories(cef_cpp_support PRIVATE
  "${_cef_client_headers}"
  "${_cef_client_source}"
)
target_compile_definitions(cef_cpp_support PRIVATE
  WRAPPING_CEF_SHARED
  CEF_STATIC
  CEF_API_VERSION=15200
)
target_compile_features(cef_cpp_support PRIVATE cxx_std_20)
if(MSVC)
  target_compile_options(cef_cpp_support PRIVATE
    "/FI${_cef_client_headers}/include/cef_static_client_names.h")
  # cef_logging.cc includes windows.h before <algorithm>; match the official
  # CEF Windows client flags so min/max macros cannot corrupt std::min/max.
  target_compile_definitions(cef_cpp_support PRIVATE
    NOMINMAX
    WIN32_LEAN_AND_MEAN
    UNICODE
    _UNICODE
  )
  target_compile_options(cef_cpp_support PRIVATE /GR- /utf-8)
  set_property(TARGET cef_cpp_support PROPERTY
    MSVC_RUNTIME_LIBRARY MultiThreaded)
else()
  target_compile_options(cef_cpp_support PRIVATE
    "SHELL:-include \"${_cef_client_headers}/include/cef_static_client_names.h\"")
endif()
set_target_properties(cef_cpp_support PROPERTIES
  POSITION_INDEPENDENT_CODE ON
)
install(TARGETS cef_cpp_support
  ARCHIVE DESTINATION lib/cef-static
)
