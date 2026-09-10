# Профиль статического ядра

Порт `cef-static` компилирует CEF/Chromium из закреплённых исходников и предоставляет C API. Это не `libcef_dll_wrapper` и не переупаковка официального DLL/SO. Успех отдельных unit-тестов или GN-конфигурации не означает успешную сборку ядра: необходимы native link/run и повторный link/run внешнего приложения из перенесённого SDK.

## Границы профиля

ОС: Windows x64 и Linux x64; engine Release; C API; системные библиотеки ОС остаются динамическими. Пакеты ресурсов и ICU/V8 data остаются отдельными файлами. Vulkan и SwiftShader выключены, ANGLE линкуется статически. Sandbox ещё не сертифицирован; тест использует только фиксированный локальный документ.

На Windows встроенный DXC в закреплённом Dawn является DLL-целью и добавлял `dxcompiler.dll.lib` в финальную линковку. Используется штатная настройка `dawn_use_built_dxc=false`, а `dawn_force_system_component_load=true` разрешает FXC только из системного каталога. Agility SDK выключен. Функции, требующие DXC/Shader Model 6, этим профилем не предоставляются. `dxcompiler.dll` и `dxil.dll` не разрешены ни в импортной таблице, ни среди модулей browser/renderer.

На Linux удалён только `enable_linux_installer` из параметров upstream-дистрибутива: installer не входит в выбранную статическую GN root-цель. `--fail-on-unused-args` сохранён; произвольные неизвестные аргументы не отбрасываются.

## Первичные исходники

CEF `708dc140cbc3286826a8abef89dc23a44ff9ea72`; Chromium `79460ebecaa5625e57a5fb679a735659e73dc687`; Dawn `ab8827bc57b176eeaa89f71324130c02d4d41145` из Chromium DEPS.

- [Chromium DEPS](https://github.com/chromium/chromium/blob/79460ebecaa5625e57a5fb679a735659e73dc687/DEPS)
- [Dawn GN feature declarations](https://dawn.googlesource.com/dawn/+/ab8827bc57b176eeaa89f71324130c02d4d41145/scripts/dawn_features.gni)
- [Dawn native target: условный DXC import](https://dawn.googlesource.com/dawn/+/ab8827bc57b176eeaa89f71324130c02d4d41145/src/dawn/native/BUILD.gn)
- [FXC: системная загрузка](https://dawn.googlesource.com/dawn/+/ab8827bc57b176eeaa89f71324130c02d4d41145/src/dawn/native/d3d/PlatformFunctions.cpp)
- [D3D12: EnsureDXC guard](https://dawn.googlesource.com/dawn/+/ab8827bc57b176eeaa89f71324130c02d4d41145/src/dawn/native/d3d12/BackendD3D12.cpp)
- [Неуспешный run с исходными блокерами](https://github.com/dobord/cef/actions/runs/34501838409)

Эти настройки устраняют обнаруженные конфигурационные зависимости; до завершения проверок готовый статический SDK не объявляется существующим.
