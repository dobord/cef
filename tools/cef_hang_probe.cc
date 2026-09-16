// Diagnostic launcher: inspect only the process this program creates.
// No process injection, memory dumps, privilege changes or exception swallowing.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <tlhelp32.h>
#include <dbghelp.h>
#include <cstdio>
#include <cstdlib>
#include <cwchar>
#include <string>
#include <vector>
#pragma comment(lib, "dbghelp.lib")

static void Trace(HANDLE process, HANDLE thread, DWORD id) {
  CONTEXT context = {};
  context.ContextFlags = CONTEXT_FULL;
  if (!GetThreadContext(thread, &context)) {
    std::printf("CONTEXT_ERROR %lu %lu\n", id, GetLastError()); return;
  }
  STACKFRAME64 frame = {};
  frame.AddrPC.Offset = context.Rip;
  frame.AddrStack.Offset = context.Rsp;
  frame.AddrFrame.Offset = context.Rbp;
  frame.AddrPC.Mode = frame.AddrStack.Mode = frame.AddrFrame.Mode = AddrModeFlat;
  DWORD64 previous_pc = 0, previous_sp = 0;
  for (unsigned i = 0; i < 64 && frame.AddrPC.Offset; ++i) {
    if (frame.AddrPC.Offset == previous_pc && frame.AddrStack.Offset == previous_sp) break;
    previous_pc = frame.AddrPC.Offset; previous_sp = frame.AddrStack.Offset;
    const DWORD64 base = SymGetModuleBase64(process, frame.AddrPC.Offset);
    alignas(SYMBOL_INFO) char storage[sizeof(SYMBOL_INFO) + 1024] = {};
    auto* symbol = reinterpret_cast<SYMBOL_INFO*>(storage);
    symbol->SizeOfStruct = sizeof(SYMBOL_INFO); symbol->MaxNameLen = 1024;
    DWORD64 displacement = 0;
    const BOOL found = SymFromAddr(process, frame.AddrPC.Offset, &displacement, symbol);
    std::printf("FRAME %lu %u 0x%llx 0x%llx 0x%llx %s +0x%llx\n",
                id, i, frame.AddrPC.Offset, base, base ? frame.AddrPC.Offset-base : 0,
                found ? symbol->Name : "<unresolved>", displacement);
    if (!StackWalk64(IMAGE_FILE_MACHINE_AMD64, process, thread, &frame, &context,
                     nullptr, SymFunctionTableAccess64, SymGetModuleBase64, nullptr)) break;
  }
}

static void Snapshot(const PROCESS_INFORMATION& process, const std::wstring& directory) {
  SymSetOptions(SYMOPT_DEFERRED_LOADS | SYMOPT_UNDNAME | SYMOPT_FAIL_CRITICAL_ERRORS |
                SYMOPT_NO_PROMPTS | SYMOPT_IGNORE_NT_SYMPATH);
  if (!SymInitializeW(process.hProcess, directory.c_str(), TRUE)) {
    std::printf("SYMBOL_INIT_ERROR %lu\n", GetLastError()); return;
  }
  HANDLE modules = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, process.dwProcessId);
  if (modules != INVALID_HANDLE_VALUE) {
    MODULEENTRY32W item = {}; item.dwSize = sizeof(item);
    if (Module32FirstW(modules, &item)) do {
      std::printf("MODULE 0x%llx %lu %ls\n",
                  reinterpret_cast<DWORD64>(item.modBaseAddr), item.modBaseSize, item.szModule);
    } while (Module32NextW(modules, &item));
    CloseHandle(modules);
  } else { std::printf("MODULE_SNAPSHOT_ERROR %lu\n", GetLastError()); }
  std::vector<DWORD> ids{process.dwThreadId};
  HANDLE threads = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
  if (threads != INVALID_HANDLE_VALUE) {
    THREADENTRY32 item = {}; item.dwSize = sizeof(item);
    if (Thread32First(threads, &item)) do {
      if (item.th32OwnerProcessID == process.dwProcessId &&
          item.th32ThreadID != process.dwThreadId && ids.size() < 128) ids.push_back(item.th32ThreadID);
    } while (Thread32Next(threads, &item));
    CloseHandle(threads);
  } else { std::printf("THREAD_SNAPSHOT_ERROR %lu\n", GetLastError()); }
  for (const DWORD id : ids) {
    HANDLE thread = OpenThread(THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION | THREAD_SUSPEND_RESUME, FALSE, id);
    if (!thread) { std::printf("OPEN_THREAD_ERROR %lu %lu\n", id, GetLastError()); continue; }
    std::printf("THREAD %lu main=%d\n", id, id == process.dwThreadId ? 1 : 0);
    if (SuspendThread(thread) != static_cast<DWORD>(-1)) {
      Trace(process.hProcess, thread, id);
      if (ResumeThread(thread) == static_cast<DWORD>(-1)) std::printf("RESUME_ERROR %lu %lu\n", id, GetLastError());
    } else { std::printf("SUSPEND_ERROR %lu %lu\n", id, GetLastError()); }
    CloseHandle(thread);
  }
  SymCleanup(process.hProcess);
}

int wmain(int argc, wchar_t** argv) {
  std::setvbuf(stdout, nullptr, _IONBF, 0);
  if (argc != 3) return 2;
  const std::wstring exe(argv[1]);
  wchar_t* end = nullptr;
  const unsigned long seconds = std::wcstoul(argv[2], &end, 10);
  if (!seconds || seconds > 120 || !end || *end || exe.size() < 3 ||
      exe[1] != L':' || exe.find_first_of(L"\"\r\n") != std::wstring::npos) return 2;
  const std::wstring directory = exe.substr(0, exe.find_last_of(L"\\/"));
  HANDLE job = CreateJobObjectW(nullptr, nullptr);
  if (!job) return 2;
  JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {};
  limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
  if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
    CloseHandle(job); return 2;
  }
  std::wstring command = L"\"" + exe + L"\" --enable-logging --log-level=0";
  STARTUPINFOW start = {}; start.cb = sizeof(start);
  PROCESS_INFORMATION process = {};
  if (!CreateProcessW(exe.c_str(), command.data(), nullptr, nullptr, FALSE,
                      CREATE_SUSPENDED, nullptr, nullptr, &start, &process)) {
    std::printf("CREATE_ERROR %lu\n", GetLastError()); CloseHandle(job); return 2;
  }
  if (!AssignProcessToJobObject(job, process.hProcess) || ResumeThread(process.hThread) == static_cast<DWORD>(-1)) {
    std::printf("JOB_OR_RESUME_ERROR %lu\n", GetLastError());
    TerminateProcess(process.hProcess, 125); CloseHandle(process.hThread); CloseHandle(process.hProcess);
    CloseHandle(job); return 2;
  }
  std::printf("CHILD %lu main_thread=%lu\n", process.dwProcessId, process.dwThreadId);
  const DWORD wait = WaitForSingleObject(process.hProcess, seconds * 1000);
  DWORD code = 125;
  if (wait == WAIT_TIMEOUT) {
    std::printf("TIMEOUT %lu seconds; diagnostic only\n", seconds);
    Snapshot(process, directory);
    TerminateJobObject(job, 124); code = 124;
  } else if (wait == WAIT_OBJECT_0) {
    if (!GetExitCodeProcess(process.hProcess, &code)) code = 125;
  } else { std::printf("WAIT_ERROR %lu\n", GetLastError()); TerminateJobObject(job, 125); }
  CloseHandle(job); // End any remaining descendants; never leave a test process running.
  WaitForSingleObject(process.hProcess, 5000);
  CloseHandle(process.hThread); CloseHandle(process.hProcess);
  std::printf("RESULT %lu\n", code);
  return code == 0 ? 0 : (code == 124 ? 124 : 1);
}
