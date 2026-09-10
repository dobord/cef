# Выборочная проверка upstream-генераторов

Исходники: оригинальный CEF commit `708dc140cbc3286826a8abef89dc23a44ff9ea72`. Локальный Linux-прогон, не GitHub Actions job.

```bash
cd tools
python3 -m unittest make_ctocpp_impl_test make_cpptoc_impl_test make_capi_header_test make_capi_versions_header_test make_config_header_test make_cmake_test
```

Результат: 24 tests, 23 passed, 1 failed. В общем golden-файле для `make_config_header` отсутствует `CEF_X11`, хотя fixture содержит `ozone_platform_x11=true` и Linux-генератор закономерно его добавляет. Это ограниченная диагностика тестового fixture, не доказательство дефекта работающего Chromium и не полный regression suite. Исходный тест не изменён.

```text
...................F....
======================================================================
FAIL: test_golden_writer_idempotence_missing_input_and_cli (make_config_header_test.MakeConfigHeaderTest.test_golden_writer_idempotence_missing_input_and_cli)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/mnt/data/cef-src/tools/make_config_header_test.py", line 45, in test_golden_writer_idempotence_missing_input_and_cli
    self.assertEqual(make_config_header(fixture),
    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                     read_golden('make_config_header', 'cef_config.h'))
                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError: '// C[1868 chars] CEF_X11 1\n#define CEF_V8_ENABLE_SANDBOX 1\n\[35 chars]H_\n' != '// C[1868 chars] CEF_V8_ENABLE_SANDBOX 1\n\n#endif  // CEF_INC[16 chars]H_\n'
Diff is 1999 characters long. Set self.maxDiff to None to see it.

----------------------------------------------------------------------
Ran 24 tests in 10.411s

FAILED (failures=1)
```

Предшествующая попытка discovery всех `*_test.py` была остановлена по лимиту выполнения; её нельзя считать завершённым или успешным тестовым прогоном.
