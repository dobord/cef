/* Static-engine integration fixture. Only a fixed, local document is loaded.
 * It deliberately uses the public C ABI, not libcef_dll_wrapper. */
#include "include/capi/cef_app_capi.h"
#include "include/capi/cef_browser_capi.h"
#include "include/capi/cef_client_capi.h"
#include "include/capi/cef_command_line_capi.h"
#include "include/capi/cef_process_message_capi.h"
#include "include/capi/cef_render_handler_capi.h"
#include "include/cef_api_hash.h"
#include "include/cef_version.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#if defined(_WIN32)
#include <windows.h>
#include <tlhelp32.h>
#include <direct.h>
typedef volatile LONG refcount_t;
static void ref_init(refcount_t* r) { *r = 1; }
static void ref_add(refcount_t* r) { InterlockedIncrement(r); }
static int ref_sub(refcount_t* r) { return (int)InterlockedDecrement(r); }
static int ref_load(refcount_t* r) { return (int)InterlockedCompareExchange(r, 0, 0); }
static int process_id(void) { return (int)GetCurrentProcessId(); }
#else
#include <stdatomic.h>
#include <unistd.h>
typedef atomic_int refcount_t;
static void ref_init(refcount_t* r) { atomic_init(r, 1); }
static void ref_add(refcount_t* r) { atomic_fetch_add(r, 1); }
static int ref_sub(refcount_t* r) { return atomic_fetch_sub(r, 1) - 1; }
static int ref_load(refcount_t* r) { return atomic_load(r); }
static int process_id(void) { return (int)getpid(); }
#endif

#define DROP(p) do { if ((p) != NULL) (p)->base.release(&(p)->base); } while (0)
#define RC_TYPE(TYPE, NAME) \
  typedef struct { TYPE api; refcount_t refs; } NAME##_object; \
  static void CEF_CALLBACK NAME##_add(cef_base_ref_counted_t* b) { \
    ref_add(&((NAME##_object*)b)->refs); \
  } \
  static int CEF_CALLBACK NAME##_release(cef_base_ref_counted_t* b) { \
    NAME##_object* o = (NAME##_object*)b; \
    int count = ref_sub(&o->refs); \
    if (count < 0) abort(); \
    if (count == 0) { free(o); return 1; } \
    return 0; \
  } \
  static int CEF_CALLBACK NAME##_one(cef_base_ref_counted_t* b) { \
    return ref_load(&((NAME##_object*)b)->refs) == 1; \
  } \
  static int CEF_CALLBACK NAME##_any(cef_base_ref_counted_t* b) { \
    return ref_load(&((NAME##_object*)b)->refs) > 0; \
  } \
  static TYPE* NAME##_new(void) { \
    NAME##_object* o = (NAME##_object*)calloc(1, sizeof(*o)); \
    if (!o) abort(); \
    o->api.base.size = sizeof(TYPE); \
    o->api.base.add_ref = NAME##_add; \
    o->api.base.release = NAME##_release; \
    o->api.base.has_one_ref = NAME##_one; \
    o->api.base.has_at_least_one_ref = NAME##_any; \
    ref_init(&o->refs); \
    return &o->api; \
  }
RC_TYPE(cef_app_t, app)
RC_TYPE(cef_browser_process_handler_t, browser_process)
RC_TYPE(cef_render_process_handler_t, render_process)
RC_TYPE(cef_client_t, client)
RC_TYPE(cef_life_span_handler_t, life)
RC_TYPE(cef_display_handler_t, display)
RC_TYPE(cef_render_handler_t, render)
RC_TYPE(cef_load_handler_t, load)

static int javascript_ok, paint_ok, renderer_pid, renderer_modules_ok;
static int closing, passed;

static cef_string_t text(const char* value) {
  cef_string_t result = {0};
  if (!cef_string_utf8_to_utf16(value, strlen(value), &result)) abort();
  return result;
}

static int text_is(const cef_string_t* value, const char* expected) {
  cef_string_utf8_t utf8 = {0};
  if (!value || !cef_string_utf16_to_utf8(value->str, value->length, &utf8))
    return 0;
  int equal = utf8.length == strlen(expected) &&
              memcmp(utf8.str, expected, utf8.length) == 0;
  cef_string_utf8_clear(&utf8);
  return equal;
}

/* Detect loaded engine DLLs in both the browser and renderer. OS libraries
 * (Win32, libc, X11/NSS/audio/graphics drivers) are not a static CEF engine. */
static int engine_modules_are_static(void) {
#if defined(_WIN32)
  static const wchar_t* forbidden[] = {
    L"libcef.dll", L"chrome_elf.dll", L"libEGL.dll", L"libGLESv2.dll",
    L"libvk_swiftshader.dll", L"vk_swiftshader.dll", L"ffmpeg.dll"
  };
  HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, GetCurrentProcessId());
  if (snapshot == INVALID_HANDLE_VALUE) return 0;
  MODULEENTRY32W item = {0}; item.dwSize = sizeof(item);
  int ok = Module32FirstW(snapshot, &item) != 0;
  if (ok) do {
    for (size_t i = 0; i < sizeof(forbidden)/sizeof(forbidden[0]); ++i) {
      if (_wcsicmp(item.szModule, forbidden[i]) == 0) ok = 0;
    }
  } while (Module32NextW(snapshot, &item));
  CloseHandle(snapshot);
  return ok;
#else
  static const char* forbidden[] = {
    "/libcef.so", "/libEGL.so", "/libGLESv2.so", "/libvk_swiftshader.so",
    "/libffmpeg.so"
  };
  FILE* maps = fopen("/proc/self/maps", "r");
  if (!maps) return 0;
  char line[8192]; int ok = 1;
  while (fgets(line, sizeof(line), maps)) {
    for (size_t i = 0; i < sizeof(forbidden)/sizeof(forbidden[0]); ++i) {
      if (strstr(line, forbidden[i])) ok = 0;
    }
  }
  if (ferror(maps)) ok = 0;
  fclose(maps);
  return ok;
#endif
}

static void close_browser(cef_browser_t* browser) {
  cef_browser_host_t* host = browser->get_host(browser);
  if (!host) abort();
  host->close_browser(host, 1);
  DROP(host);
}

static void finish(cef_browser_t* browser) {
  if (closing || !javascript_ok || !paint_ok || renderer_pid <= 0 ||
      renderer_pid == process_id() || !renderer_modules_ok) return;
  closing = 1;
  int modules = engine_modules_are_static();
  if (modules) {
    FILE* proof = fopen("smoke-result.json", "w");
    if (proof) {
      int written = fprintf(proof,
        "{\"cef\":\"%s\",\"engine\":\"static\",\"interface\":\"capi\","
        "\"javascript\":true,\"paint\":true,\"browser_pid\":%d,"
        "\"renderer_pid\":%d,\"browser_modules_clean\":true,"
        "\"renderer_modules_clean\":true,\"sandbox_verified\":false}\n",
        CEF_VERSION, process_id(), renderer_pid);
      int flushed = fclose(proof);
      passed = written > 0 && flushed == 0;
    }
  }
  close_browser(browser);
}

static void CEF_CALLBACK before_close(cef_life_span_handler_t* self,
                                      cef_browser_t* browser) {
  (void)self; DROP(browser); cef_quit_message_loop();
}
static void CEF_CALLBACK title_change(cef_display_handler_t* self,
                                      cef_browser_t* browser, const cef_string_t* title) {
  (void)self;
  if (text_is(title, "CEF_STATIC_42")) javascript_ok = 1;
  finish(browser); DROP(browser);
}
static void CEF_CALLBACK view_rect(cef_render_handler_t* self,
                                   cef_browser_t* browser, cef_rect_t* rect) {
  (void)self; rect->x = rect->y = 0; rect->width = 320; rect->height = 240;
  DROP(browser);
}
static void CEF_CALLBACK paint(cef_render_handler_t* self, cef_browser_t* browser,
                               cef_paint_element_type_t type, size_t count,
                               const cef_rect_t* dirty, const void* buffer,
                               int width, int height) {
  (void)self; (void)count; (void)dirty;
  if (type == PET_VIEW && buffer && width == 320 && height == 240) {
    const uint8_t* p = (const uint8_t*)buffer + (120*width+160)*4;
    if (p[0] == 39 && p[1] == 27 && p[2] == 13 && p[3] == 255) paint_ok = 1;
  }
  finish(browser); DROP(browser);
}
static void CEF_CALLBACK load_error(cef_load_handler_t* self, cef_browser_t* browser,
                                    cef_frame_t* frame, cef_errorcode_t code,
                                    const cef_string_t* error, const cef_string_t* url) {
  (void)self; (void)error; (void)url;
  if (frame->is_main(frame) && code != ERR_ABORTED && !closing) {
    fprintf(stderr, "LOAD_ERROR %d\n", (int)code);
    closing = 1; close_browser(browser);
  }
  DROP(frame); DROP(browser);
}
static int CEF_CALLBACK message_received(cef_client_t* self, cef_browser_t* browser,
                                         cef_frame_t* frame, cef_process_id_t source,
                                         cef_process_message_t* message) {
  (void)self;
  cef_string_userfree_t name = message->get_name(message);
  int handled = source == PID_RENDERER && text_is(name, "static-proof");
  cef_string_userfree_free(name);
  if (handled) {
    cef_list_value_t* args = message->get_argument_list(message);
    renderer_pid = args->get_int(args, 0);
    renderer_modules_ok = args->get_bool(args, 1);
    DROP(args); finish(browser);
  }
  DROP(message); DROP(frame); DROP(browser);
  return handled;
}
static cef_life_span_handler_t* CEF_CALLBACK get_life(cef_client_t* self) {
  (void)self; cef_life_span_handler_t* h = life_new(); h->on_before_close = before_close; return h;
}
static cef_display_handler_t* CEF_CALLBACK get_display(cef_client_t* self) {
  (void)self; cef_display_handler_t* h = display_new(); h->on_title_change = title_change; return h;
}
static cef_render_handler_t* CEF_CALLBACK get_render(cef_client_t* self) {
  (void)self; cef_render_handler_t* h = render_new(); h->get_view_rect = view_rect; h->on_paint = paint; return h;
}
static cef_load_handler_t* CEF_CALLBACK get_load(cef_client_t* self) {
  (void)self; cef_load_handler_t* h = load_new(); h->on_load_error = load_error; return h;
}
static void CEF_CALLBACK context_initialized(cef_browser_process_handler_t* self) {
  (void)self;
  cef_client_t* c = client_new();
  c->get_life_span_handler = get_life; c->get_display_handler = get_display;
  c->get_render_handler = get_render; c->get_load_handler = get_load;
  c->on_process_message_received = message_received;
  cef_window_info_t window = {0}; window.size = sizeof(window);
  window.windowless_rendering_enabled = 1;
  window.bounds.width = 320; window.bounds.height = 240;
  window.runtime_style = CEF_RUNTIME_STYLE_ALLOY;
  cef_browser_settings_t settings = {0}; settings.size = sizeof(settings);
  settings.windowless_frame_rate = 30;
  /* Percent escapes are literal. Spaces are never encoded as '+'. */
  cef_string_t url = text("data:text/html;charset=utf-8,%3Chtml%3E%3Cbody%20style='margin:0;background:rgb(13,27,39)'%3ECEF%3Cscript%3Edocument.title='CEF_STATIC_'%2BString(6*7)%3C/script%3E%3C/body%3E%3C/html%3E");
  int ok = cef_browser_host_create_browser(&window, c, &url, &settings, NULL, NULL);
  cef_string_clear(&url);
  /* The creation reference of c was transferred to the CEF call. */
  if (!ok) { fputs("CREATE_BROWSER_FAILED\n", stderr); cef_quit_message_loop(); }
}
static void CEF_CALLBACK context_created(cef_render_process_handler_t* self,
                                         cef_browser_t* browser, cef_frame_t* frame,
                                         cef_v8_context_t* context) {
  (void)self;
  if (frame->is_main(frame)) {
    cef_string_t name = text("static-proof");
    cef_process_message_t* msg = cef_process_message_create(&name);
    cef_string_clear(&name);
    cef_list_value_t* args = msg->get_argument_list(msg);
    args->set_int(args, 0, process_id());
    args->set_bool(args, 1, engine_modules_are_static());
    DROP(args);
    frame->send_process_message(frame, PID_BROWSER, msg); /* transfers msg */
  }
  DROP(context); DROP(frame); DROP(browser);
}
static cef_browser_process_handler_t* CEF_CALLBACK get_browser_process(cef_app_t* self) {
  (void)self; cef_browser_process_handler_t* h = browser_process_new();
  h->on_context_initialized = context_initialized; return h;
}
static cef_render_process_handler_t* CEF_CALLBACK get_render_process(cef_app_t* self) {
  (void)self; cef_render_process_handler_t* h = render_process_new();
  h->on_context_created = context_created; return h;
}
static void CEF_CALLBACK command_line(cef_app_t* self, const cef_string_t* type,
                                     cef_command_line_t* command) {
  (void)self; (void)type;
  static const char* switches[] = {"disable-gpu", "disable-background-networking",
    "disable-component-update", "disable-extensions", "no-first-run", "no-sandbox"};
  for (size_t i = 0; i < sizeof(switches)/sizeof(switches[0]); ++i) {
    cef_string_t value = text(switches[i]);
    command->append_switch(command, &value); cef_string_clear(&value);
  }
  cef_string_t key = text("force-device-scale-factor"), value = text("1");
  command->append_switch_with_value(command, &key, &value);
  cef_string_clear(&key); cef_string_clear(&value);
  key = text("force-color-profile"); value = text("srgb");
  command->append_switch_with_value(command, &key, &value);
  cef_string_clear(&key); cef_string_clear(&value); DROP(command);
}

int main(int argc, char** argv) {
  const char* hash = cef_api_hash(CEF_API_VERSION, 0);
  if (!hash || strcmp(hash, CEF_API_HASH_PLATFORM)) {
    fputs("CEF_API_HASH_MISMATCH\n", stderr); return 10;
  }
  if (!engine_modules_are_static()) {
    fputs("SHARED_ENGINE_MODULE_DETECTED\n", stderr); return 11;
  }
  cef_main_args_t args = {0};
#if defined(_WIN32)
  (void)argc; (void)argv; args.instance = GetModuleHandleW(NULL);
#else
  args.argc = argc; args.argv = argv;
#endif
  cef_app_t* a = app_new();
  a->get_browser_process_handler = get_browser_process;
  a->get_render_process_handler = get_render_process;
  a->on_before_command_line_processing = command_line;
  a->base.add_ref(&a->base);
  int code = cef_execute_process(&args, a, NULL);
  if (code >= 0) { DROP(a); return code; }
  cef_settings_t settings = {0}; settings.size = sizeof(settings);
  settings.no_sandbox = 1; /* Fixed local CI fixture ONLY. Not a sandbox claim. */
  settings.windowless_rendering_enabled = 1;
  char directory[4096], cache[8192];
#if defined(_WIN32)
  if (!_getcwd(directory, sizeof(directory))) { DROP(a); return 12; }
#else
  if (!getcwd(directory, sizeof(directory))) { DROP(a); return 12; }
#endif
  int length = snprintf(cache, sizeof(cache), "%s/test-cache", directory);
  if (length < 0 || (size_t)length >= sizeof(cache)) { DROP(a); return 13; }
  settings.root_cache_path = text(cache);
  int initialized = cef_initialize(&args, &settings, a, NULL);
  cef_string_clear(&settings.root_cache_path);
  if (!initialized) { fputs("CEF_INITIALIZE_FAILED\n", stderr); return 14; }
  cef_run_message_loop();
  cef_shutdown();
  puts(passed ? "CEF_STATIC_SMOKE_PASS" : "CEF_STATIC_SMOKE_FAIL");
  return passed ? 0 : 15;
}
