# CEF 152 — overlay-порт vcpkg

**Это статическая C++-обёртка с обязательным динамическим ядром CEF. Полностью статический `libcef` не реализован.** Пакет требует `libcef.dll` на Windows или `libcef.so` на Linux. Feature `static-engine` намеренно завершается ошибкой, чтобы такое требование не было молча подменено гибридной сборкой.

Оригинальные исходники CEF сохранены в `main`; интеграция находится в `vcpkg`. Версия: `152.0.6+g708dc14+chromium-152.0.7977.83`. Проверены `x64-windows-static` и `x64-linux`, Debug и Release wrapper. Оба используют официальный Release runtime через C ABI.

[Разбор исходников и статической линковки](STATIC-ANALYSIS.md) · [Фактические результаты проверок](STATUS.md)

## Сборка через vcpkg

Ниже команды для каталога, в котором будут созданы соседние `cef-port` и `vcpkg`. Нужны Git, Python 3, CMake 3.21+ и C++20-компилятор. Windows CI проверен с Visual Studio 2022, Linux CI — с Ubuntu 24.04/GCC.

### Windows x64, PowerShell

```powershell
git clone --branch vcpkg https://github.com/dobord/cef.git cef-port
git clone https://github.com/microsoft/vcpkg.git vcpkg
git -C vcpkg checkout 3723ec118c8354290925feb58d021a9205a3e772
.\vcpkg\bootstrap-vcpkg.bat -disableMetrics
$ports = (Resolve-Path .\cef-port\vcpkg\ports).Path
.\vcpkg\vcpkg.exe install cef:x64-windows-static --classic "--overlay-ports=$ports"

$prefix = (Resolve-Path .\vcpkg\installed\x64-windows-static).Path
cmake -S .\cef-port\vcpkg\smoke -B .\build-win -G "Visual Studio 17 2022" -A x64 `
  "-DCMAKE_PREFIX_PATH=$prefix" `
  '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded$<$<CONFIG:Debug>:Debug>'
cmake --build .\build-win --config Release
Push-Location .\build-win\Release
.\cef_smoke.exe
Pop-Location
```

### Linux x64, Bash

```bash
sudo apt-get update
sudo apt-get install -y build-essential ninja-build cmake python3 git curl zip unzip tar \
  libx11-dev libnss3 libgbm1 libgtk-3-0t64 libasound2t64 libxss1 xvfb xauth

git clone --branch vcpkg https://github.com/dobord/cef.git cef-port
git clone https://github.com/microsoft/vcpkg.git vcpkg
git -C vcpkg checkout 3723ec118c8354290925feb58d021a9205a3e772
./vcpkg/bootstrap-vcpkg.sh -disableMetrics
./vcpkg/vcpkg install cef:x64-linux --classic --overlay-ports="$PWD/cef-port/vcpkg/ports"

cmake -S cef-port/vcpkg/smoke -B build-linux -G 'Ninja Multi-Config' \
  -DCMAKE_PREFIX_PATH="$PWD/vcpkg/installed/x64-linux"
cmake --build build-linux --config Release
(cd build-linux/Release && xvfb-run -a ./cef_smoke)
```

Для Debug замените конфигурацию сборки и каталог запуска на `Debug`. Успешный smoke-тест завершится с кодом 0, выведет `CEF_SMOKE_PASS` и запишет `smoke-result.json`.

**Smoke-приложение запускает только фиксированный локальный документ без sandbox. Не используйте его настройки для недоверенного контента. Windows bootstrap/sandbox-интеграция не включена в этот порт; sandbox не проверен ни на одной платформе.**

## Подключение к своему CMake-проекту

```cmake
cmake_minimum_required(VERSION 3.21)
project(my_app LANGUAGES CXX)

find_package(cef 152.0.6 EXACT CONFIG REQUIRED)
add_executable(my_app main.cc)
target_link_libraries(my_app PRIVATE CEF::wrapper)
cef_deploy_runtime(my_app)
```

Заголовки подключаются в стандартном для CEF виде: `#include "include/cef_app.h"`. Target передаёт C++20, `CEF_API_VERSION=15200` и необходимые зависимости. `CEF::runtime` является **SHARED IMPORTED** target. Переменные пакета явно задают `CEF_WRAPPER_LINKAGE=STATIC`, `CEF_ENGINE_LINKAGE=SHARED`.

`cef_deploy_runtime` копирует `tools/cef` к executable и задаёт `$ORIGIN` на Linux. Не удаляйте runtime, resource packs, локализации, ICU data или V8 snapshot из развёртывания. При подключении по `CMAKE_PREFIX_PATH` на Windows настройки CRT приложения должны совпадать с wrapper: `/MT` для Release, `/MTd` для Debug в проверенном triplet. Обычный vcpkg toolchain также может использоваться вместо `CMAKE_PREFIX_PATH`.

## Использование готового SDK

Архивы — результат `vcpkg export --raw` с добавленными lock-файлом и build receipt. После проверки SHA-256 распакуйте SDK и передайте `installed/<triplet>` внутри него как `CMAKE_PREFIX_PATH`. Дополнительно в экспорте присутствует `scripts/buildsystems/vcpkg.cmake`.

Проверенные архивы:

```text
cef-152.0.6-x64-linux-static-wrapper-shared-runtime.zip
SHA256: 45eb5ab554ccd45be72f3f54219a93c32a69be267f75681bf0c057077712acbb

cef-152.0.6-x64-windows-static-static-wrapper-shared-runtime.zip
SHA256: f8af5c23facda08d37b1ef0a0056f4fc897fd624e470785ec874a68dc4853344
```

Архивы находятся в [Actions run сборки](https://github.com/dobord/cef/actions/runs/34433831085) и прикреплены к [неопубликованному техническому черновику](https://github.com/dobord/cef/releases/tag/untagged-fa6e59d2e0758718946e). Доступ к draft требует прав на репозиторий. Они также проверены как входы для сборки и запуска на новых runners, где первоначальной установки vcpkg не было: [archive-consumer run](https://github.com/dobord/cef/actions/runs/34434605354).

## CI и релизы

`.github/workflows/vcpkg.yml` содержит матрицу Windows/Linux и job `publish`, запускаемый для `release.published` только после успеха обеих сборок. Он прикрепляет два ZIP и два SHA-256 файла, не перезаписывая существующие assets. Пакеты остаются явно гибридными — этот workflow не создаёт статического ядра Chromium.

Релиз должен использовать **новый тег на коммите ветки `vcpkg`**, содержащем workflow. Не создавайте релиз интеграции по `main`: эта ветка намеренно является оригинальным upstream. В интерфейсе создания релиза выберите target `vcpkg`, либо предварительно создайте тег на нужном коммите этой ветки.

Технический черновик `cef-152.0.6-hybrid-ci-validation-20260910` создан только для проверки доставки файлов и не опубликован. Не публикуйте его как production-релиз: там уже есть assets, а release-job не выполняет overwrite. Само событие `release.published` в проверочной процедуре не генерировалось; отдельно подтверждены матрица build/smoke, потребление ZIP на новых runners и загрузка assets в draft.

## Требование полностью статического ядра

```bash
vcpkg install 'cef[static-engine]:x64-linux' --overlay-ports=/path/to/cef/vcpkg/ports
```

Этот запрос должен завершиться явной ошибкой. Он не даёт статический engine. Не удаляйте защиту и не переименовывайте shared runtime в «static build». Необходимая отдельная переработка GN-целей, exports, ABI-границы, упаковки и subprocess/sandbox описана в `STATIC-ANALYSIS.md`; она не реализована и не проверена текущим портом.
