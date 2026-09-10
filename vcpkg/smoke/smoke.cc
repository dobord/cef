#include "include/base/cef_compiler_specific.h"
#include "include/cef_app.h"
#include "include/cef_browser.h"
#include "include/cef_client.h"
#include "include/cef_command_line.h"
#include "include/cef_parser.h"
#include "include/cef_process_message.h"
#include "include/cef_render_handler.h"
#include "include/cef_render_process_handler.h"
#include "include/cef_version.h"
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#if defined(_WIN32)
#include <windows.h>
#else
#include <unistd.h>
#endif
namespace {
int ProcessId() {
#if defined(_WIN32)
  return static_cast<int>(GetCurrentProcessId());
#else
  return static_cast<int>(getpid());
#endif
}
bool success = false;
class Client final : public CefClient, public CefLifeSpanHandler,
                     public CefDisplayHandler, public CefLoadHandler,
                     public CefRenderHandler {
 public:
  CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
  CefRefPtr<CefDisplayHandler> GetDisplayHandler() override { return this; }
  CefRefPtr<CefLoadHandler> GetLoadHandler() override { return this; }
  CefRefPtr<CefRenderHandler> GetRenderHandler() override { return this; }
  void GetViewRect(CefRefPtr<CefBrowser>, CefRect& rect) override { rect = CefRect(0, 0, 320, 240); }
  bool GetScreenInfo(CefRefPtr<CefBrowser>, CefScreenInfo& info) override {
    info.device_scale_factor = 1;
    info.rect = CefRect(0, 0, 320, 240);
    info.available_rect = info.rect;
    return true;
  }
  void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
    std::cout << "BROWSER_CREATED pid=" << ProcessId() << std::endl;
    browser->GetHost()->WasResized();
  }
  void OnTitleChange(CefRefPtr<CefBrowser> browser, const CefString& title) override {
    std::cout << "TITLE " << title.ToString() << std::endl;
    js_ = js_ || title.ToString() == "CEF_SMOKE_42";
    Finish(browser);
  }
  void OnPaint(CefRefPtr<CefBrowser> browser, PaintElementType type,
               const RectList&, const void* buffer, int width, int height) override {
    if (type != PET_VIEW || !buffer || width != 320 || height != 240) {
      std::cout << "UNEXPECTED_PAINT " << width << "x" << height << std::endl;
      return;
    }
    const auto* p = static_cast<const uint8_t*>(buffer) + (120 * width + 160) * 4;
    const bool expected = p[0] == 39 && p[1] == 27 && p[2] == 13 && p[3] == 255;
    if (paints_++ < 4 || (expected && !painted_)) {
      std::cout << "PAINT bgra=" << int(p[0]) << "," << int(p[1]) << ","
                << int(p[2]) << "," << int(p[3]) << " expected=" << expected << std::endl;
    }
    painted_ = painted_ || expected;
    Finish(browser);
  }
  bool OnProcessMessageReceived(CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame>,
                                CefProcessId source, CefRefPtr<CefProcessMessage> message) override {
    if (source != PID_RENDERER || message->GetName() != "cef-smoke-renderer") return false;
    renderer_ = message->GetArgumentList()->GetInt(0);
    std::cout << "RENDERER_IPC pid=" << renderer_ << std::endl;
    Finish(browser);
    return true;
  }
  void OnLoadEnd(CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame> frame, int code) override {
    if (frame->IsMain()) {
      std::cout << "LOAD_END status=" << code << std::endl;
      browser->GetHost()->Invalidate(PET_VIEW);
    }
  }
  void OnLoadError(CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame> frame,
                   ErrorCode code, const CefString& text, const CefString&) override {
    if (frame->IsMain() && code != ERR_ABORTED) {
      std::cerr << "Load failed: " << code << " " << text.ToString() << std::endl;
      closing_ = true;
      browser->GetHost()->CloseBrowser(true);
    }
  }
  void OnBeforeClose(CefRefPtr<CefBrowser>) override {
    std::cout << "BROWSER_CLOSED" << std::endl;
    CefQuitMessageLoop();
  }
 private:
  void Finish(CefRefPtr<CefBrowser> browser) {
    if (closing_ || !js_ || !painted_ || renderer_ <= 0 || renderer_ == ProcessId()) return;
    closing_ = true;
    std::ofstream proof("smoke-result.json");
    proof << "{\"cef\":\"" << CEF_VERSION << "\",\"engine\":\"shared\","
          << "\"wrapper\":\"static\",\"javascript\":true,\"paint\":true,"
          << "\"browser_pid\":" << ProcessId() << ",\"renderer_pid\":" << renderer_ << "}\n";
    proof.close();
    success = static_cast<bool>(proof);
    std::cout << "ALL_MILESTONES_VERIFIED" << std::endl;
    browser->GetHost()->CloseBrowser(true);
  }
  bool js_ = false, painted_ = false, closing_ = false;
  int renderer_ = 0;
  int paints_ = 0;
  IMPLEMENT_REFCOUNTING(Client);
};
class App final : public CefApp, public CefBrowserProcessHandler, public CefRenderProcessHandler {
 public:
  CefRefPtr<CefBrowserProcessHandler> GetBrowserProcessHandler() override { return this; }
  CefRefPtr<CefRenderProcessHandler> GetRenderProcessHandler() override { return this; }
  void OnBeforeCommandLineProcessing(const CefString& type, CefRefPtr<CefCommandLine> cmd) override {
    std::cout << "PROCESS pid=" << ProcessId() << " type=" << type.ToString() << std::endl;
    cmd->AppendSwitch("disable-gpu");
    cmd->AppendSwitch("disable-background-networking");
    cmd->AppendSwitch("disable-component-update");
    cmd->AppendSwitch("disable-extensions");
    cmd->AppendSwitch("no-first-run");
    cmd->AppendSwitchWithValue("force-color-profile", "srgb");
    cmd->AppendSwitchWithValue("force-device-scale-factor", "1");
    cmd->AppendSwitch("no-sandbox");  // Controlled local CI fixture ONLY.
  }
  void OnContextInitialized() override {
    std::cout << "BROWSER_CONTEXT_INITIALIZED" << std::endl;
    CefWindowInfo window;
    window.SetAsWindowless(kNullWindowHandle);
    CefBrowserSettings browser;
    browser.windowless_frame_rate = 30;
    const std::string html = "<html><body style='margin:0;background:rgb(13,27,39)'>CEF"
        "<script>document.title='CEF_SMOKE_'+String(6*7)</script></body></html>";
    // data: URLs use percent encoding, NOT application/x-www-form-urlencoded '+' spaces.
    const std::string url = "data:text/html;charset=utf-8," + CefURIEncode(html, false).ToString();
    if (!CefBrowserHost::CreateBrowser(window, new Client, url, browser, nullptr, nullptr)) {
      std::cerr << "CreateBrowser failed" << std::endl;
      CefQuitMessageLoop();
    }
  }
  void OnContextCreated(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame,
                        CefRefPtr<CefV8Context>) override {
    if (!frame->IsMain()) return;
    std::cout << "RENDERER_CONTEXT_CREATED pid=" << ProcessId() << std::endl;
    auto message = CefProcessMessage::Create("cef-smoke-renderer");
    message->GetArgumentList()->SetInt(0, ProcessId());
    frame->SendProcessMessage(PID_BROWSER, message);
  }
 private:
  IMPLEMENT_REFCOUNTING(App);
};
}
// Chromium can reset the stack canary after fork in a Linux child process.
NO_STACK_PROTECTOR
int main(int argc, char** argv) {
#if defined(_WIN32)
  CefMainArgs args(GetModuleHandle(nullptr));
#else
  CefMainArgs args(argc, argv);
#endif
  CefRefPtr<App> app = new App;
  const int child = CefExecuteProcess(args, app, nullptr);
  if (child >= 0) return child;
  CefSettings settings;
  settings.no_sandbox = true;  // Never reuse this for browsing untrusted content.
  settings.windowless_rendering_enabled = true;
  CefString(&settings.root_cache_path) = (std::filesystem::current_path() / "test-cache").string();
  if (!CefInitialize(args, settings, app, nullptr)) return 2;
  CefRunMessageLoop();
  app = nullptr;
  CefShutdown();
  std::cout << (success ? "CEF_SMOKE_PASS" : "CEF_SMOKE_FAIL") << std::endl;
  return success ? 0 : 3;
}
