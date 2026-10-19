# Финансовый анализатор — этап 4: первый дополнительный банк

Программа **0.5.0**, SQLite-схема **2**. Локальный импорт текстовых PDF-выписок
кредитной карты Сбера и рублёвых справок о движении средств Ozon Банка
с защитой от повторов и архивированием. Финансовая классификация, отчёты,
другие форматы и OCR пока отсутствуют.
Требуется Python 3.10+ с venv. Версия программы хранится в program/version.py;
версия схемы и правила версионируются отдельно.

## Установка для разработчика

PowerShell, из корня новой распакованной папки проекта:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Используются sqlite3 из Python, openpyxl для справочников, pypdf для PDF и pytest.
Для Linux/macOS создать окружение через `python3 -m venv .venv`, затем заменить
путь Python в командах на `.venv/bin/python`.

## Обновление с версии 0.3.0 или 0.4.0 с сохранением базы

Старую программу закрыть, папку и данные не удалять. Новую версию распаковать
отдельно. **Не выполнять init до copy**, если переносится старая база.

```powershell
.\.venv\Scripts\python.exe -m program.database copy --source "ПАПКА_ПРЕЖНЕЙ_ВЕРСИИ\database\finance.db"
.\.venv\Scripts\python.exe -m program.database init
.\.venv\Scripts\python.exe -m program.database check
.\.venv\Scripts\python.exe -m program.database info
```

copy создаёт проверенную копию, не меняя исходник и не заменяя существующую
целевую базу. init обновляет копию до схемы 2 с проверенным backup. Существующие
операции и решения сохраняются. `.venv` из старой папки не переносить.
При переносе с 0.4.0 также скопировать целиком содержимое старой папки `archive`
в новую `archive`, сохранив вложенные папки и имена. Перенести личные справочники
из `config`, если они есть. Старые файлы не удалять. Для 0.4.0 → 0.5.0 схема
остаётся 2; миграция не требуется, init проверяет перенесённую базу.
Автоматическое обновление всей установки пока не реализовано.

Для **новой установки без старой базы** после установки зависимостей достаточно:

```powershell
.\.venv\Scripts\python.exe -m program.database init
.\.venv\Scripts\python.exe -m program.database check
```

## Работа с выписками

Скопировать PDF в input, открыть «Запустить анализ.bat», выбрать 1. Каждый
файл предварительно показывается и подтверждается словом `ДА`. Новые строки
сохраняются, известные не дублируются. Исходник переносится в archive/statements
только после записи и проверки архивной копии. Пункт 0 завершает программу.
Пункты 2–7 пока остаются заглушками. При ошибке подготовки BAT показывает
сообщение и ждёт клавишу; обычный старт меню не создаёт и не обновляет базу.

```powershell
.\.venv\Scripts\python.exe -m program.main
.\.venv\Scripts\python.exe -m program.importers preview "input\выписка.pdf"
.\.venv\Scripts\python.exe -m program.importers import "input\выписка.pdf" --confirm
.\.venv\Scripts\python.exe -m program.importers recover
.\.venv\Scripts\python.exe -m program.database info
```

Предпросмотр и отмена ничего не записывают. Для отдельной базы есть --database.
При незавершённом архивировании исходник сохраняется и выводится предупреждение;
recover или следующая подтверждённая попытка завершает действие без дублей.
Для работы использовать один активный экземпляр программы.

## Справочники и файлы

Пустые шаблоны: config/categories.template.xlsx, config/own_accounts.template.xlsx.
Личные копии categories.xlsx и own_accounts.xlsx исключены из Git.

```powershell
.\.venv\Scripts\python.exe -m program.database import-categories --file config\categories.xlsx
.\.venv\Scripts\python.exe -m program.database import-accounts --file config\own_accounts.xlsx
```

Код и тесты хранятся в Git. Рабочие базы, настоящие входные PDF, архивы, backup,
логи и .venv — нет. Единственная тестовая DB в tests/fixtures полностью вымышленная.
Никакие финансовые категории и собственные счета не зашиваются в Python.

- [Устройство базового хранилища и XLSX](docs/database.md).
- [Накопительный импорт, схема 2, архив и перенос базы](docs/cumulative_import.md).
- [Протокол этапа 3](docs/validation_stage3.md).
- [Исторический протокол этапа 2](docs/validation_stage2.md).

- [Поддерживаемый формат Ozon Банка и проверка версии 0.5.0](docs/import_ozon_pdf.md).
