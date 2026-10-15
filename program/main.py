"""Консольный каркас; финансовые действия пока отсутствуют."""
from pathlib import Path

from program.version import __version__

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TITLE = "Финансовый анализатор"
OPTIONS = (
    "Загрузить новые выписки",
    "Разобрать неопределённые",
    "Найти и изменить операцию",
    "Сформировать Excel-отчёт",
    "Посмотреть правила",
    "Контроль базы",
    "Создать резервную копию",
)


def build_menu() -> str:
    lines = [TITLE, f"Версия {__version__}"]
    lines.extend(
        f"{number}. {label}" + (" [первый PDF-импорт]" if number == 1 else " [ещё не реализовано]")
        for number, label in enumerate(OPTIONS, start=1)
    )
    lines.append("0. Выход")
    return "\n".join(lines)


def main() -> int:
    while True:
        print(build_menu())
        try:
            choice = input("Выберите пункт: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nВыход.")
            return 0
        if choice == "0":
            print("Выход.")
            return 0
        if choice == "1":
            from program.ui.console import import_first_pdf
            import_first_pdf(PROJECT_ROOT)
        elif choice in {str(number) for number in range(2, len(OPTIONS) + 1)}:
            print("Ещё не реализовано.\n")
        else:
            print("Неизвестный пункт. Выберите номер из меню.\n")


if __name__ == "__main__":
    raise SystemExit(main())
