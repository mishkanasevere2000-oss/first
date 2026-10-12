@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
if errorlevel 1 (
    echo Не удалось открыть папку проекта.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo Виртуальное окружение не найдено. Подготовьте среду по README.md.
    pause
    popd
    exit /b 1
)
".venv\Scripts\python.exe" -X utf8 -m program.main
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
    echo Не удалось запустить программу или выполнение завершилось с ошибкой.
    echo Проверьте установку Python и среду по README.md.
    pause
)
popd
exit /b %APP_EXIT%
