"""Read a text PDF once, then detect a registered bank format from its content."""
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .models import StatementError
from .parsers import sber_credit_pdf_v1


def read_statement(path):
    path = Path(path)
    if path.suffix.lower() != '.pdf':
        raise StatementError('На этапе 2 поддерживается только PDF-выписка кредитной карты Сбера.')
    try:
        source = path.read_bytes()
        if not source.startswith(b'%PDF-'):
            raise StatementError('Файл не является PDF.')
        reader = PdfReader(BytesIO(source), strict=True)
        if reader.is_encrypted:
            raise StatementError('PDF защищён паролем. Нужна незашифрованная выписка.')
        pages = [page.extract_text(extraction_mode='layout') if '/Contents' in page else ''
                 for page in reader.pages]
    except (PyPdfError, ValueError, TypeError, KeyError) as error:
        if isinstance(error, StatementError):
            raise
        raise StatementError('Не удалось прочитать структуру PDF.') from error
    if not pages or not all(page and page.strip() for page in pages):
        raise StatementError('В PDF нет читаемого текста на всех страницах. Сканы пока не поддерживаются.')
    if sber_credit_pdf_v1.matches(pages[0]):
        return sber_credit_pdf_v1.parse_pages(pages)
    raise StatementError('Банк или формат PDF не распознан. Импорт не выполнен.')
