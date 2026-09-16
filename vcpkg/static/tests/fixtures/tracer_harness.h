// Native regression adapter, NOT the real PartitionAlloc heap or CEF runtime.
// The tested translation unit and InternalAllocator templates are full pinned
// upstream files. std::map, std::vector, mutexes and allocation hooks are real.
#pragma once
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>
#include <utility>
#include <vector>
#define PA_BUILDFLAG(flag) 1
#define PA_CHECK(condition) do { if (!(condition)) std::abort(); } while (false)
#define PA_COMPONENT_EXPORT(component)

void FixtureHook(unsigned event);
inline std::atomic<unsigned> internal_allocs{0}, internal_frees{0};
namespace base::internal {
class InstanceTracer {
 public:
  static std::atomic<uint64_t> counter_;
  static void TraceImpl(uint64_t, bool, uintptr_t);
  static void UntraceImpl(uint64_t);
  static std::vector<std::array<const void*, 32>> GetStackTracesForDanglingRefs(uintptr_t);
  static std::vector<std::array<const void*, 32>> GetStackTracesForAddressForTest(const void*);
};
}
namespace partition_alloc {
enum class AllocFlags { kNoHooks };
enum class FreeFlags { kNoHooks };
class PartitionRoot {
 public:
  static void* InSlotMetadataPointerFromSlotStartAndSize(uintptr_t address, size_t) {
    return reinterpret_cast<void*>(address);
  }
  template <AllocFlags flags> void* Alloc(size_t count) {
    static_assert(flags == AllocFlags::kNoHooks);
    ++internal_allocs;
    void* result = std::malloc(count ? count : 1);
    PA_CHECK(result);
    return result;
  }
  template <FreeFlags flags> void Free(void* ptr) {
    static_assert(flags == FreeFlags::kNoHooks);
    ++internal_frees;
    std::free(ptr);
  }
};
struct SlotAddressAndSize {
  uintptr_t slot_start;
  size_t size;
  static SlotAddressAndSize FromBRPPool(uintptr_t address) { return {address, 1}; }
};
namespace internal {
inline PartitionRoot& InternalAllocatorRoot() { static PartitionRoot root; return root; }
namespace base {
template <class T> class NoDestructor {
 public:
  NoDestructor() { new (storage_) T(); }
  T& operator*() { return *std::launder(reinterpret_cast<T*>(storage_)); }
 private:
  alignas(T) unsigned char storage_[sizeof(T)];
};
namespace debug {
inline void CollectStackTrace(const void** frames, size_t count) {
  FixtureHook(4);
  if (count) frames[0] = reinterpret_cast<const void*>(uintptr_t{0x1234});
}
}
}
}
}
