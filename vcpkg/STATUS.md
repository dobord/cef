# Статус реализации и проверок

## Главное ограничение

**Полностью статическое ядро CEF не реализовано и не собрано. Исходное требование выполнено частично.** Готовый пакет содержит статический C++ wrapper и обязательные `libcef.dll` / `libcef.so`. Это явно указано в CMake, описании порта, именах ZIP и build receipt.

## Зафиксированные исходники

`main`: `708dc140cbc3286826a8abef89dc23a44ff9ea72` — оригинальный upstream CEF `152.0.6+g708dc14+chromium-152.0.7977.83`.

Проверенный код интеграции: `b6c3a6210050a15ccf1d9f65cabb005cbe3ac1b4`. Следующие изменения документации не изменяют код этого проверенного пакета.

## Фактические проверки

| Проверка | Результат | Доказательство |
|---|---|---|
| Оригинальный upstream commit/tree | Успех | [Import run](https://github.com/dobord/cef/actions/runs/34432152781) |
| Размеры, SHA-1 и SHA-512 official minimal архивов | Успех | [Audit run](https://github.com/dobord/cef/actions/runs/34432281264) |
| Windows x64: сборка wrapper/порта, Debug и Release link/run | Успех | [Build run](https://github.com/dobord/cef/actions/runs/34433831085) |
| Linux x64: сборка wrapper/порта, Debug и Release link/run | Успех | [Build run](https://github.com/dobord/cef/actions/runs/34433831085) |
| Windows/Linux: ZIP на новых runners, Debug и Release link/run со скрытым SDK | Успех | [Archive consumer run](https://github.com/dobord/cef/actions/runs/34434605354) |
| Загрузка двух SDK, SHA-256 и JSON-отчётов в draft release | Успех, 6 assets | [Upload run](https://github.com/dobord/cef/actions/runs/34434605354) |
| Полная статическая сборка ядра из CEF/Chromium sources | Не реализована, не проверена | Не выдаётся за выполненную |
| Sandbox | Не проверен | Smoke использует только локальный документ с `no_sandbox` |
| Production `release.published` event | Не воспроизводился | Workflow настроен; загрузка assets проверена отдельно в draft |
| Полный набор тестов CEF/Chromium | Не выполнен | Smoke не заменяет regression suite |

## Проверка уже упакованных SDK

Отчёты fresh-runner проверки содержат следующие пары PID (browser / renderer):

| Платформа | Release | Debug |
|---|---|---|
| Linux x64 | `3029 / 3087` | `3147 / 3206` |
| Windows x64 | `1060 / 6508` | `2928 / 3648` |

Во всех четырёх запусках подтверждены `javascript=true`, `paint=true`, другой PID renderer и завершение процесса с кодом 0. На Linux проверено разрешение `libcef.so` из каталога перенесённого executable. На Windows `dumpbin /DEPENDENTS` подтвердил динамический `libcef.dll`; зависимости самого тестового executable от `VCRUNTIME140` / `MSVCP140` не обнаружены.

Проверочный workflow сохранён в [коммите 0e863a7](https://github.com/dobord/cef/blob/0e863a71024905044ad8e50351481e0d7d23aac7/.github/workflows/validate-release-upload.yml). Его временная ветка после успешной проверки удалена. Ветки проекта — `main` и `vcpkg`.

## Черновик релиза

[CEF 152.0.6 — hybrid SDK CI validation (unpublished)](https://github.com/dobord/cef/releases/tag/untagged-fa6e59d2e0758718946e) остаётся **draft**. Он доступен владельцу/пользователям с соответствующими правами и не является опубликованным релизом.

| SDK | Размер ZIP, байт | SHA-256 |
|---|---:|---|
| x64-linux | 381712174 | `45eb5ab554ccd45be72f3f54219a93c32a69be267f75681bf0c057077712acbb` |
| x64-windows-static | 299283946 | `f8af5c23facda08d37b1ef0a0056f4fc897fd624e470785ec874a68dc4853344` |

SHA-256 опубликованных в draft assets совпали с проверенными архивами. Внутри каждого SDK находится `build-receipt.json`; отдельные `archive-consumer-*.json` прикреплены к draft. Во всех отчётах `full_static_engine_verified` равно `false`.

## Не скрытые неуспешные проверки

Первый bootstrap-run не учитывал старый формат версии в общем индексе CEF; выбор версии исправлен. Первый build/smoke-run собрал и слинковал порт, но не завершил smoke из-за ошибки HTML `data:` URL. Исправление percent-encoding прошло обе платформы; исходный [неуспешный run](https://github.com/dobord/cef/actions/runs/34432860932) сохранён.

Выборочный локальный прогон 24 upstream Python generator-тестов: 23 успеха, 1 failure. Ошибка — platform-dependent `CEF_X11` в golden-тесте `make_config_header_test`. Этот исходный тест не менялся; полный regression suite не объявляется успешным. Подробности в [UPSTREAM-TESTS.md](UPSTREAM-TESTS.md).
