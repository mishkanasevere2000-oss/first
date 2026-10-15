# Финансовый анализатор — этап 1

Локальное консольное приложение 0.2.0 с хранилищем SQLite. Финансовая обработка
выписок пока отсутствует; основное меню сохраняет заглушки.
Требуется Python 3.10 или новее с модулем venv.

## Windows (PowerShell, из корня проекта)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m program.main
```

После подготовки среды приложение можно запускать двойным щелчком по
`Запустить анализ.bat`. Лаунчер сам выбирает корень проекта; при отсутствии
окружения или ошибке запуска показывает сообщение и ждёт нажатия клавиши.

## Linux / macOS

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m program.main
```

Версия хранится только в `program/version.py`. Для чтения XLSX-справочников
используется openpyxl; pytest используется для тестирования. Каталоги output, backup и logs создаются
по мере необходимости будущими этапами и исключены из Git; после клонирования
их можно создать командой `mkdir output backup logs`.

Проверка на Windows: запустить BAT из Проводника, выбрать 1 (ожидается
«ещё не реализовано»), затем 0. Повторить из другого рабочего каталога и из
пути с пробелами/кириллицей. Временно переименовать .venv и проверить сообщение
об отсутствующем окружении; вернуть имя. Ошибка запуска должна оставлять окно
открытым. База finance.db создаётся только явной командой `program.database init`,
а не запуском BAT или основного меню.

## Этап 1 — база данных (программа 0.2.0)

Установить обновлённые зависимости и выполнить проверки (PowerShell, корень проекта):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m program.database init
.\.venv\Scripts\python.exe -m program.database check
```

`init` создаёт пустую `database/finance.db`, повторный запуск только проверяет её.
`check` проверяет существующую базу, не создавая новую. Главное меню базу не создаёт.
На Linux/macOS заменить `.\.venv\Scripts\python.exe` на `.venv/bin/python`.

Дополнительные команды разработчика:

```powershell
.\.venv\Scripts\python.exe -m program.database check --database "путь\finance.db"
.\.venv\Scripts\python.exe -m program.database import-categories --file config\categories.xlsx
.\.venv\Scripts\python.exe -m program.database import-accounts --file config\own_accounts.xlsx
```

Пустые XLSX-шаблоны находятся в `config/categories.template.xlsx` и
`config/own_accounts.template.xlsx`. Формат колонок, ограничения и схема описаны
в [docs/database.md](docs/database.md). Личные справочники сохранять под именами
без `.template`; эти пути исключены из Git. Реальные выписки пока не загружать.

### Обновление с этапа 0

Скачать новый ZIP из GitHub и распаковать **в отдельную новую папку**. Не удалять
старую папку и не заменять каталоги database/config/archive/backup при обновлении.
В новой папке создать свою `.venv`, установить requirements и выполнить команды
проверки выше. Не переносить `.venv` из другой папки или с другого компьютера.
Подготовка новой папки не меняет старую установку. До переноса рабочей базы
и справочников нужен отдельный план обновления с резервной копией; на этапе 0
производственной базы ещё не было.

Меню теперь показывает версию 0.2.0. Его финансовые пункты всё ещё заглушки:
банковские парсеры, классификация и отчёты относятся к следующим этапам.
