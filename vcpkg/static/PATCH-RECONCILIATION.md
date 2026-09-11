# Согласование патча статического порта с текущей веткой

База: `dobord/cef`, `static-engine`, `8e75ce325ef05cd55dd46dd1565d0480bf1279e3`.
Исходный патч был рассчитан на `ea3c66b60a278dd3ac500bca8ca64789efb42935`.
Новый патч заменяет прежний; их нельзя применять последовательно.

## Уже внесённые изменения

После старой базы ветка получила два коммита: `11aefd9a182402670113927f3e14f66775fc435d`
и `8e75ce325ef05cd55dd46dd1565d0480bf1279e3`. Ветка `static-tools-fix` указывает на
первый из них и уже является предком `static-engine`. Отдельное слияние не нужно.

`acquire_git.cmake`, вызов tools в основном порте, вспомогательный порт
`cef-static-toolcheck`, скрипт `toolcheck.py` и существующие `test_source_tools.py`
сохранены без изменений. Git не получается второй раз другим механизмом.

В `source_build.py` сохранены `git_program()`, `python_script()`, `check_tools()`,
`transient_network_failure()`, `run_network()` и полная текущая последовательность
`prepare()`. Обёртка запуска Python сохранена не только для automation, но и для
translator, upstream patcher и static patcher. Это важно для Windows Python с
изолированным `sys.path`; прямой запуск старым способом снова ломал бы sibling imports.

После automation по-прежнему обязательны dependency sync, runhooks patch,
runhooks и повторная проверка закреплённых SHA. Ошибка любого этапа не создаёт
`prepared.json`. Старые дублирующие `source_sync()` и `run_network_step()` из
прежнего патча не переносятся. Тесты на эти устаревшие функции заменены проверками
сохранённого нового пути подготовки исходников.

## Дополнительные изменения поверх новой базы

CLI поддерживает `tools`, `prepare`, `check`, `regressions`, `build`. `tools`
не скачивает Chromium. `check` генерирует и проверяет граф и linker flags без
компиляции. Компиляция отдельных regression objects выполняется только явной
командой `regressions`. `build` не пропускает сборку и запуск native executable.

Добавлены JSON-статусы subprocess, отдельный статус тайм-аута, ранняя проверка
экспортируемых linker flags, запрет логических значений вместо PID и проверка
импортов внешнего Windows-потребителя. Сохраняются строгие проверки static engine,
отдельного renderer, JavaScript, off-screen paint, runtime-модулей и SHA архивов.

Повторное использование Chromium/Ninja workspace отделено от свежего каталога
экспорта и внешнего consumer. Каталоги SDK и исходников скрываются с восстановлением
при ошибке. При невозможности восстановления исходные данные не удаляются.
Пакет и receipt не сертифицируются одним лишь наличием кэша.

Workflow сохраняет ранний `toolcheck.py`, fallback ключа ccache и release gates.
Контрактные тесты назначены обеим hosted-платформам. Source jobs не выполняют PR-код
на постоянных машинах. Деструктивная очистка предустановленных пакетов ограничена
GitHub-hosted Linux runner. На self-hosted зависимости устанавливаются отдельно.
Параметры runner/workspace остались опциональными; настройки репозитория не изменяются.

| Переменная репозитория | Назначение / значение по умолчанию |
|---|---|
| `CEF_STATIC_LINUX_RUNNER_LABELS` | JSON-массив меток; `["ubuntu-24.04"]` |
| `CEF_STATIC_WINDOWS_RUNNER_LABELS` | JSON-массив меток; `["windows-2022"]` |
| `CEF_STATIC_LINUX_WORK` / `CEF_STATIC_WINDOWS_WORK` | Абсолютный отдельный workspace; по умолчанию `runner.temp/cef-static` |
| `CEF_STATIC_JOB_TIMEOUT_MINUTES` | `350`; лимит runner всё равно действует |
| `CEF_STATIC_BUILD_TIMEOUT_SECONDS` | `18000`; регулирует subprocess, не лимит GitHub |
| `CEF_STATIC_JOBS` | `4`; параллелизм компиляции |

## Что этот патч не объявляет выполненным

Это согласование интеграционных исправлений, а не подтверждение полной статической
сборки. GN-патчи движка (`patch_source.py`), C API fixture (`smoke.c`), закреплённые
версии и профиль Release/C API остаются прежними. Ресурсы — отдельные файлы,
системные библиотеки — динамические; sandbox не сертифицирован.

Последний проверенный native run `34522469597` на исходной базе прошёл toolcheck,
подготовку источников и конфигурацию на Windows/Linux, но завершился failure.
Linux: отсутствует `sync/sync.h` при компиляции
`dawn_ozone_image_representation.cc`. Windows: сборка прервана по тайм-ауту 18000 секунд.
Этот патч не содержит исправления зависимости libsync и не доказывает, что увеличенного
тайм-аута хватит для сборки. Native build/link/run и экспорт SDK требуют отдельной проверки.

## Источники

- [Нативный Git и изолированный Python, 11aefd9](https://github.com/dobord/cef/commit/11aefd9a182402670113927f3e14f66775fc435d).
- [Предварительная проверка инструментов, 8e75ce3](https://github.com/dobord/cef/commit/8e75ce325ef05cd55dd46dd1565d0480bf1279e3).
- [Последний native run исходной базы](https://github.com/dobord/cef/actions/runs/34522469597).
- [Python: sys.path и _pth isolation](https://docs.python.org/3/library/sys_path_init.html#pth-files).
- [Git apply: проверка, index и трёхстороннее применение](https://git-scm.com/docs/git-apply).
