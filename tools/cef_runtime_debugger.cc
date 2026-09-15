// Diagnostic launcher for one explicitly supplied native test executable.
// Never attach to other processes or swallow application exceptions.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dbghelp.h>
#include <cstdio>
#include <string>
#include <vector>
#pragma comment(lib, "dbghelp.lib")

static void Trace(HANDLE process, HANDLE thread) {
  CONTEXT context = {};
  context.ContextFlags = CONTEXT_FULL;
  if (!GetThreadContext(thread, &context)) {
    std::printf("GET_CONTEXT_FAILED %lu\n", GetLastError()); return;
  }
  STACKFRAME64 frame = {};
  frame.AddrPC.Offset = context.Rip;
  frame.AddrStack.Offset = context.Rsp;
  frame.AddrFrame.Offset = context.Rbp;
  frame.AddrPC.Mode = frame.AddrStack.Mode = frame.AddrFrame.Mode = AddrModeFlat;
  for (unsigned i = 0; i < 48 && frame.AddrPC.Offset; ++i) {
    alignas(SYMBOL_INFO) char storage[sizeof(SYMBOL_INFO) + 1024] = {};
    auto symbol = reinterpret_cast<SYMBOL_INFO*>(storage);
    symbol->SizeOfStruct = sizeof(SYMBOL_INFO); symbol->MaxNameLen = 1024;
    DWORD64 displacement = 0;
    const bool found = SymFromAddr(process, frame.AddrPC.Offset, &displacement, symbol) != FALSE;
    std::printf("FRAME %u 0x%llx %s +0x%llx\n", i, frame.AddrPC.Offset,
                found ? symbol->Name : "<unresolved>", displacement);
    if (!StackWalk64(IMAGE_FILE_MACHINE_AMD64, process, thread, &frame, &context,
                     nullptr, SymFunctionTableAccess64, SymGetModuleBase64, nullptr)) break;
  }
  std::fflush(stdout);
}

int wmain(int argc, wchar_t** argv) {
  if (argc != 2) { std::fprintf(stderr, "usage: cef_runtime_debugger.exe absolute-executable\n"); return 2; }
  const std::wstring exe(argv[1]);
  if (exe.find(L'"') != std::wstring::npos || exe.find(L':') != 1) return 2;
  const std::wstring directory = exe.substr(0, exe.find_last_of(L"\\/"));
  std::wstring command = L"\"" + exe + L"\" --enable-logging=stderr --log-severity=verbose";
  STARTUPINFOW start = {}; start.cb = sizeof(start);
  PROCESS_INFORMATION process = {};
  if (!CreateProcessW(exe.c_str(), command.data(), nullptr, nullptr, FALSE,
                      DEBUG_ONLY_THIS_PROCESS, nullptr, directory.c_str(), &start, &process)) {
    std::printf("CREATE_FAILED %lu\n", GetLastError()); return 2;
  }
  DebugSetProcessKillOnExit(TRUE);
  SymSetOptions(SYMOPT_DEFERRED_LOADS | SYMOPT_UNDNAME | SYMOPT_FAIL_CRITICAL_ERRORS | SYMOPT_LOAD_LINES);
  const bool symbols = SymInitializeW(process.hProcess, directory.c_str(), FALSE) != FALSE;
  bool initial_breakpoint = true;
  const ULONGLONG deadline = GetTickCount64() + 180000;
  DWORD exit_code = 1;
  for (;;) {
    DEBUG_EVENT event = {};
    if (!WaitForDebugEventEx(&event, 1000)) {
      if (GetLastError() == ERROR_SEM_TIMEOUT && GetTickCount64() < deadline) continue;
      std::printf("DEBUG_TIMEOUT_OR_ERROR %lu\n", GetLastError());
      TerminateProcess(process.hProcess, 124); exit_code = 124; break;
    }
    DWORD disposition = DBG_CONTINUE;
    bool done = false;
    switch (event.dwDebugEventCode) {
      case CREATE_PROCESS_DEBUG_EVENT: {
        auto& info = event.u.CreateProcessInfo;
        if (symbols) SymLoadModuleExW(process.hProcess, info.hFile, exe.c_str(), nullptr,
                          reinterpret_cast<DWORD64>(info.lpBaseOfImage), 0, nullptr, 0);
        std::printf("IMAGE_BASE 0x%llx\n", reinterpret_cast<DWORD64>(info.lpBaseOfImage));
        if (info.hFile) CloseHandle(info.hFile);
        break;
      }
      case LOAD_DLL_DEBUG_EVENT: {
        auto& info = event.u.LoadDll;
        wchar_t path[32768] = {};
        if (info.hFile) {
          GetFinalPathNameByHandleW(info.hFile, path, 32768, FILE_NAME_NORMALIZED);
          if (symbols) SymLoadModuleExW(process.hProcess, info.hFile, path, nullptr,
                            reinterpret_cast<DWORD64>(info.lpBaseOfDll), 0, nullptr, 0);
          CloseHandle(info.hFile);
        }
        break;
      }
      case UNLOAD_DLL_DEBUG_EVENT:
        if (symbols) SymUnloadModule64(process.hProcess, reinterpret_cast<DWORD64>(event.u.UnloadDll.lpBaseOfDll));
        break;
      case OUTPUT_DEBUG_STRING_EVENT: {
        auto& info = event.u.DebugString;
        const SIZE_T bytes = (static_cast<SIZE_T>(info.nDebugStringLength) * (info.fUnicode ? 2 : 1));
        if (bytes && bytes < 65536) {
          std::vector<char> data(bytes + 2, 0); SIZE_T read = 0;
          if (ReadProcessMemory(process.hProcess, info.lpDebugStringData, data.data(), bytes, &read)) {
            if (info.fUnicode) std::printf("DEBUG_STRING %ls\n", reinterpret_cast<const wchar_t*>(data.data()));
            else std::printf("DEBUG_STRING %s\n", data.data());
          }
        }
        break;
      }
      case EXCEPTION_DEBUG_EVENT: {
        auto& info = event.u.Exception;
        const DWORD code = info.ExceptionRecord.ExceptionCode;
        if (code == EXCEPTION_BREAKPOINT && initial_breakpoint && info.dwFirstChance) {
          initial_breakpoint = false; break; // OS loader's one initial debug break only.
        }
        std::printf("EXCEPTION code=0x%08lx first=%lu address=0x%llx\n", code,
                    info.dwFirstChance, reinterpret_cast<DWORD64>(info.ExceptionRecord.ExceptionAddress));
        HANDLE thread = OpenThread(THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, event.dwThreadId);
        if (thread) { Trace(process.hProcess, thread); CloseHandle(thread); }
        disposition = DBG_EXCEPTION_NOT_HANDLED; // preserve the application's real failure
        break;
      }
      case EXIT_PROCESS_DEBUG_EVENT:
        exit_code = event.u.ExitProcess.dwExitCode; done = true;
        std::printf("PROCESS_EXIT 0x%08lx\n", exit_code);
        break;
      default: break;
    }
    std::fflush(stdout);
    if (!ContinueDebugEvent(event.dwProcessId, event.dwThreadId, disposition)) {
      std::printf("CONTINUE_FAILED %lu\n", GetLastError());
      TerminateProcess(process.hProcess, 125); exit_code = 125; break;
    }
    if (done) break;
    if (GetTickCount64() >= deadline) {
      TerminateProcess(process.hProcess, 124); exit_code = 124; break;
    }
  }
  if (symbols) SymCleanup(process.hProcess);
  CloseHandle(process.hThread); CloseHandle(process.hProcess);
  return exit_code == 0 ? 0 : 1;
}
