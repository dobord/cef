cmake_minimum_required(VERSION 3.24)
project(cef_static_cpp_support LANGUAGES CXX)

if(NOT DEFINED CEF_RECIPE_SOURCE OR NOT IS_DIRECTORY "${CEF_RECIPE_SOURCE}")
  message(FATAL_ERROR "CEF_RECIPE_SOURCE must name the pinned CEF recipe")
endif()
if(NOT DEFINED CEF_PACKAGE_PREFIX OR NOT IS_DIRECTORY "${CEF_PACKAGE_PREFIX}/include/cef-static")
  message(FATAL_ERROR "CEF_PACKAGE_PREFIX must contain the exported cef-static headers")
endif()

add_library(cef_cpp_support STATIC
  "${CEF_RECIPE_SOURCE}/libcef_dll/base/cef_logging.cc"
)
target_include_directories(cef_cpp_support PRIVATE
  "${CEF_PACKAGE_PREFIX}/include/cef-static"
)
target_compile_definitions(cef_cpp_support PRIVATE
  CEF_STATIC
  CEF_API_VERSION=15200
)
target_compile_features(cef_cpp_support PRIVATE cxx_std_23)
if(MSVC)
  set_property(TARGET cef_cpp_support PROPERTY
    MSVC_RUNTIME_LIBRARY MultiThreaded)
endif()
set_target_properties(cef_cpp_support PROPERTIES
  POSITION_INDEPENDENT_CODE ON
)
install(TARGETS cef_cpp_support
  ARCHIVE DESTINATION lib/cef-static
)
