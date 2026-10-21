import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_import_has_no_filesystem_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module = importlib.import_module('program.main')
    importlib.reload(module)
    assert list(tmp_path.iterdir()) == []
    assert module.PROJECT_ROOT == ROOT


def test_version_and_menu():
    from program.main import build_menu
    from program.version import __version__
    assert __version__ == '0.12.0'
    menu = build_menu()
    assert f'Версия {__version__}' in menu
    assert 'Финансовый анализатор' in menu
    assert '0. Выход' in menu
    assert menu.count('[ещё не реализовано]') == 2


@pytest.mark.parametrize('choice', [str(i) for i in (4, 7)])
def test_placeholder_changes_no_files(choice, tmp_path, monkeypatch, capsys):
    from program.main import main
    monkeypatch.chdir(tmp_path)
    answers = iter([choice, '0'])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    assert main() == 0
    assert 'Ещё не реализовано.' in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_invalid_input_then_exit(monkeypatch, capsys):
    from program.main import main
    answers = iter(['invalid', '0'])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    assert main() == 0
    assert 'Неизвестный пункт' in capsys.readouterr().out


def test_eof_exits(monkeypatch):
    from program.main import main
    def eof(_):
        raise EOFError
    monkeypatch.setattr('builtins.input', eof)
    assert main() == 0


def test_cli_process(tmp_path):
    environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8='1')
    result = subprocess.run(
        [sys.executable, '-m', 'program.main'], input='4\n0\n',
        text=True, encoding='utf-8', capture_output=True,
        cwd=tmp_path, env=environment, timeout=10,
    )
    assert result.returncode == 0
    from program.version import __version__
    assert f'Версия {__version__}' in result.stdout
    assert 'Ещё не реализовано.' in result.stdout
    assert 'Выход.' in result.stdout
    assert result.stderr == ''
    assert list(tmp_path.iterdir()) == []
