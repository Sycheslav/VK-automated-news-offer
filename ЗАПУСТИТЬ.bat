@echo off
chcp 65001 >nul
echo ========================================
echo    VK Suggester - Автозапуск
echo ========================================
echo.

:: Проверка наличия Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден!
    echo Установите Python 3.8+ с https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo [1/4] Проверка Python... OK
echo.

:: Проверка/создание виртуального окружения
if not exist "venv\" (
    echo [2/4] Создание виртуального окружения...
    python -m venv venv
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось создать виртуальное окружение!
        pause
        exit /b 1
    )
    echo Виртуальное окружение создано!
) else (
    echo [2/4] Виртуальное окружение... OK
)
echo.

:: Активация виртуального окружения
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ОШИБКА] Не удалось активировать виртуальное окружение!
    pause
    exit /b 1
)

:: Установка/обновление зависимостей
echo [3/4] Проверка и установка зависимостей...
pip install -q -r requirements.txt
if errorlevel 1 (
    echo [ОШИБКА] Не удалось установить зависимости!
    pause
    exit /b 1
)
echo Зависимости установлены!
echo.

:: Запуск приложения и открытие браузера
echo [4/4] Запуск приложения...
echo.
echo ========================================
echo    Приложение запускается...
echo    Откроется в браузере через 3 секунды
echo ========================================
echo.
echo URL: http://localhost:5000
echo.
echo Для остановки нажмите Ctrl+C
echo.

:: Открываем браузер в фоне через 3 секунды
start /B cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:5000"

:: Запускаем приложение (блокирующий режим)
python app.py

:: Если приложение завершилось, показываем сообщение
echo.
echo ========================================
echo Приложение остановлено
echo ========================================
pause
