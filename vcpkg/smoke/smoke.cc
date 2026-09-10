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
  void OnTitleChange(CefRefPtr<CefBrowser> browser, const CefString& title) override {
    js_ = title.ToString() == "CEF_SMOKE_42";
    Finish(browser);
  }
  void OnPaint(CefRefPtr<CefBrowser> browser, PaintElementType type,
               const RectList&, const void* buffer, int width, int height) override {
    if (type == PET_VIEW && buffer && width == 320 && height == 240) {
      const auto* p = static_cast<const uint8_t*>(buffer) + (120 * width + 160) * 4;
      painted_ = p[0] == 39 && p[1] == 27 && p[2] == 13 && p[3] == 255;
      Finish(browser);
    }
  }
  bool OnProcessMessageReceived(CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame>,
                                CefProcessId source, CefRefPtr<CefProcessMessage> message) override {
    if (source != PID_RENDERER || message->GetName() != "cef-smoke-renderer") return false;
    renderer_ = message->GetArgumentList()->GetInt(0);
    Finish(browser);
    return true;
  }
  void OnLoadError(CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame> frame,
                   ErrorCode code, const CefString& text, const CefString&) override {
    if (frame->IsMain() && code != ERR_ABORTED) {
      std::cerr << "Load failed: " << code << " " << text.ToString() << std::endl;
      closing_ = true;
      browser->GetHost()->CloseBrowser(true);
    }
  }
  void OnBeforeClose(CefRefPtr<CefBrowser>) override { CefQuitMessageLoop(); }
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
    browser->GetHost()->CloseBrowser(true);
  }
  bool js_ = false, painted_ = false, closing_ = false;
  int renderer_ = 0;
  IMPLEMENT_REFCOUNTING(Client);
};
class App final : public CefApp, public CefBrowserProcessHandler, public CefRenderProcessHandler {
 public:
  CefRefPtr<CefBrowserProcessHandler> GetBrowserProcessHandler() override { return this; }
  CefRefPtr<CefRenderProcessHandler> GetRenderProcessHandler() override { return this; }
  void OnBeforeCommandLineProcessing(const CefString&, CefRefPtr<CefCommandLine> cmd) override {
    cmd->AppendSwitch("disable-gpu");
    cmd->AppendSwitch("disable-background-networking");
    cmd->AppendSwitch("disable-component-update");
    cmd->AppendSwitch("disable-extensions");
    cmd->AppendSwitch("no-first-run");
    cmd->AppendSwitch("no-sandbox");  // Controlled local CI fixture ONLY.
  }
  void OnContextInitialized() override {
    CefWindowInfo window;
    window.SetAsWindowless(kNullWindowHandle);
    CefBrowserSettings browser;
    browser.windowless_frame_rate = 30;
    const std::string html = "<html><body style='margin:0;background:rgb(13,27,39)'>CEF"
        "<script>document.title='CEF_SMOKE_'+String(6*7)</script></body></html>";
    const std::string url = "data:text/html;charset=utf-8," + CefURIEncode(html, true).ToString();
    if (!CefBrowserHost::CreateBrowser(window, new Client, url, browser, nullptr, nullptr)) {
      std::cerr << "CreateBrowser failed" << std::endl;
      CefQuitMessageLoop();
    }
  }
  void OnContextCreated(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame,
                        CefRefPtr<CefV8Context>) override {
    if (!frame->IsMain()) return;
    auto message = CefProcessMessage::Create("cef-smoke-renderer");
    message->GetArgumentList()->SetInt(0, ProcessId());
    frame->SendProcessMessage(PID_BROWSER, message);
  }
 private:
  IMPLEMENT_REFCOUNTING(App);
};
}
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
