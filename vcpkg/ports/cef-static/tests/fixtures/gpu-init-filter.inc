#if BUILDFLAG(IS_OZONE)
  ui::OzonePlatform::GetInstance()->AfterSandboxEntry();
  [[maybe_unused]] auto* factory =
      ui::OzonePlatform::GetInstance()->GetSurfaceFactoryOzone();
  bool filter_set = false;
#if BUILDFLAG(ENABLE_VULKAN)
  if (gpu_feature_info_.status_values[GPU_FEATURE_TYPE_VULKAN] ==
          kGpuFeatureStatusEnabled &&
      factory->SupportsDrmModifiersFilter()) {
    CHECK(!filter_set);
    DCHECK(vulkan_implementation_ &&
           vulkan_implementation_->GetVulkanInstance() &&
           vulkan_implementation_->GetVulkanInstance()->vk_instance() !=
               VK_NULL_HANDLE);
    factory->SetDrmModifiersFilter(std::make_unique<DrmModifiersFilterVulkan>(
        vulkan_implementation_.get()));
    filter_set = true;
  }
#endif  // BUILDFLAG(ENABLE_VULKAN)
#if BUILDFLAG(SKIA_USE_DAWN) && BUILDFLAG(IS_CHROMEOS)
  if (dawn_context_provider_ && factory->SupportsDrmModifiersFilter()) {
    CHECK(!filter_set);
    factory->SetDrmModifiersFilter(std::make_unique<DrmModifiersFilterDawn>(
        dawn_context_provider_->GetDevice().GetAdapter()));
    filter_set = true;
  }
#endif  // BUILDFLAG(SKIA_USE_DAWN) && BUILDFLAG(IS_CHROMEOS)
#endif  // BUILDFLAG(IS_OZONE)
