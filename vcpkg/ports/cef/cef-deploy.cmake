function(cef_deploy_runtime target)
    if(NOT TARGET "${target}")
        message(FATAL_ERROR "cef_deploy_runtime requires an existing target")
    endif()
    add_custom_command(TARGET "${target}" POST_BUILD
        COMMAND "${CMAKE_COMMAND}" -E copy_directory "${CEF_RUNTIME_DIR}" "$<TARGET_FILE_DIR:${target}>"
        VERBATIM)
    if(UNIX AND NOT APPLE)
        # The test/application must run without the SDK's original absolute location.
        set_target_properties("${target}" PROPERTIES
            BUILD_WITH_INSTALL_RPATH TRUE
            INSTALL_RPATH "$ORIGIN"
            INSTALL_RPATH_USE_LINK_PATH FALSE)
    endif()
endfunction()
