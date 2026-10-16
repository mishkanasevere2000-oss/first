"""Two-phase source preservation: verified snapshot, DB commit, archive, cleanup."""
import hashlib
import os
from pathlib import Path
import tempfile

from program.database.connection import transaction
from program.database.imports import ImportLedger


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('Некорректный путь архива в базе.')
    return path


def snapshot(root, data, sha256):
    folder = Path(root) / '.pending'
    folder.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=sha256 + '_', suffix='.pdf', dir=folder)
    path = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if digest(path.read_bytes()) != sha256:
            raise OSError('Контрольная сумма временной копии не совпала.')
        return path.relative_to(Path(root)).as_posix()
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def finalize(connection, root, sha256):
    """A post-commit archive error never pretends that financial data rolled back."""
    ledger=ImportLedger(connection)
    row=ledger.source(sha256)
    try:
        with transaction(connection):
            target = safe_path(root, row['archive_path'])
            pending = safe_path(root, row['pending_path']) if row['pending_path'] else None
            if target.exists():
                if digest(target.read_bytes()) != sha256:
                    raise OSError('В архиве уже есть файл с другим содержимым. Он не перезаписан.')
            else:
                if pending is None or not pending.exists() or digest(pending.read_bytes()) != sha256:
                    raise OSError('Нет проверенной временной копии для завершения архивирования.')
                data = pending.read_bytes()
                target.parent.mkdir(parents=True, exist_ok=True)
                created = False
                try:
                    with target.open('xb') as output:
                        created = True
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    if digest(target.read_bytes()) != sha256:
                        raise OSError('Контрольная сумма архива не совпала.')
                except BaseException:
                    if created:
                        target.unlink(missing_ok=True)
                    raise
            ledger.mark_archived(sha256)
        # Delete only our generated snapshot, only after the durable archive is verified.
        if pending and pending.exists() and digest(pending.read_bytes()) == sha256:
            pending.unlink()
        return target, None
    except (OSError, ValueError) as error:
        ledger.mark_archive_error(sha256,error)
        return None, 'Операции учтены, но архивирование не завершено: ' + str(error)


def recover(connection, root, *, skip_sha256=None):
    warnings = []
    ledger=ImportLedger(connection)
    ledger.interrupted()
    for sha256 in ledger.pending_archives():
        if sha256==skip_sha256:
            continue
        _,warning=finalize(connection,root,sha256)
        if warning:
            warnings.append(warning)
    return warnings


def remove_original(path, archived, sha256):
    path, archived = Path(path), Path(archived)
    if path.resolve() == archived.resolve():
        return False, None
    try:
        if path.exists() and digest(path.read_bytes()) == sha256:
            path.unlink()
            return True, None
        return False, 'Исходный файл изменён или отсутствует; он не удалялся.'
    except OSError as error:
        return False, 'Архив проверен, но исходник остался на месте: ' + str(error)
