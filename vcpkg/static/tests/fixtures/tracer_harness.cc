// Deterministic allocator callback tests with actual pinned tracer source.
#include "tracer_harness.h"
#include <cstring>
#include <thread>
#include <system_error>
#include "instance_tracer_under_test.cc"

static std::atomic<unsigned> hook_mask{0}, hook_calls{0};
static thread_local bool in_hook = false;
void FixtureHook(unsigned event) {
  if ((hook_mask.load() & event) && !in_hook) {
    in_hook = true;
    ++hook_calls;
    std::printf("REENTER %u\n", event);
    // Model destruction of a raw_ptr in an allocator/shim callback. The owner
    // is absent, so this call needs only the tracer mutex and never allocates.
    try {
      base::internal::InstanceTracer::UntraceImpl(999999);
    } catch (const std::system_error& error) {
      // MSVC may diagnose same-thread std::mutex relock rather than wait as
      // Chromium libc++ does. Accept only this exact negative-control reason.
      if (error.code() == std::errc::resource_deadlock_would_occur) {
        std::puts("EXPECTED_SELF_DEADLOCK");
        std::_Exit(86);
      }
      throw;
    }
    in_hook = false;
  }
}
void* operator new(size_t size) {
  FixtureHook(1);
  void* ptr = std::malloc(size ? size : 1);
  if (!ptr) throw std::bad_alloc();
  return ptr;
}
void operator delete(void* ptr) noexcept { FixtureHook(2); std::free(ptr); }
void operator delete(void* ptr, size_t) noexcept { ::operator delete(ptr); }

int main(int argc, char** argv) {
  std::setvbuf(stdout, nullptr, _IONBF, 0);
  if (argc != 2) return 2;
  using Tracer = base::internal::InstanceTracer;
  const uintptr_t address = 0x1000;
  Tracer::TraceImpl(1, false, address);
  Tracer::TraceImpl(2, true, address);
  Tracer::TraceImpl(3, false, 0x2000);
  if (!std::strcmp(argv[1], "free")) {
    hook_mask = 2;
    Tracer::UntraceImpl(1);
  } else if (!std::strcmp(argv[1], "alloc")) {
    hook_mask = 1;
    Tracer::TraceImpl(4, false, address);
  } else if (!std::strcmp(argv[1], "stack")) {
    hook_mask = 4;
    Tracer::TraceImpl(4, false, address);
    PA_CHECK(hook_calls > 0);
  } else if (!std::strcmp(argv[1], "snapshot")) {
    hook_mask = 1 | 2;
    auto result = Tracer::GetStackTracesForDanglingRefs(address);
    PA_CHECK(result.size() == 1 && result[0][0] == reinterpret_cast<void*>(uintptr_t{0x1234}));
    PA_CHECK(hook_calls > 0);  // public vector still uses ordinary allocation
  } else if (!std::strcmp(argv[1], "semantics")) {
    Tracer::TraceImpl(1, true, 0x2000); // duplicate must not replace old Info
    auto result = Tracer::GetStackTracesForAddressForTest(reinterpret_cast<void*>(address));
    PA_CHECK(result.size() == 1);
    Tracer::UntraceImpl(9876); // absent owner is permitted
    std::vector<std::thread> threads;
    for (uint64_t worker = 0; worker < 6; ++worker) {
      threads.emplace_back([worker] {
        for (uint64_t iteration = 0; iteration < 300; ++iteration) {
          const uint64_t owner = 100 + worker * 300 + iteration;
          const uintptr_t addr = 0x3000 + worker;
          Tracer::TraceImpl(owner, false, addr);
          auto traces = Tracer::GetStackTracesForDanglingRefs(addr);
          PA_CHECK(traces.size() == 1);
          Tracer::UntraceImpl(owner);
        }
      });
    }
    for (auto& thread : threads) thread.join();
    Tracer::UntraceImpl(1);
    PA_CHECK(Tracer::GetStackTracesForDanglingRefs(address).empty());
  } else {
    return 2;
  }
  hook_mask = 0;
  for (uint64_t owner = 1; owner <= 4; ++owner) Tracer::UntraceImpl(owner);
#ifdef EXPECT_INTERNAL_ALLOCATOR
  PA_CHECK(internal_allocs > 0 && internal_allocs == internal_frees);
#endif
  std::printf("PASS %s hooks=%u internal_allocs=%u internal_frees=%u\n", argv[1],
              hook_calls.load(), internal_allocs.load(), internal_frees.load());
  return 0;
}
