"""Read a text PDF once, then detect a registered bank format from its content."""
from io import BytesIO
from dataclasses import replace
import hashlib
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .models import StatementError
from .parsers import sber_credit_pdf_v1, sber_debit_pdf_v1, ozon_pdf_v1, vtb_card_pdf_v1, rshb_card_pdf_v1, tbank_card_pdf_v1, alfa_account_pdf_v1


def read_statement(path):
    path = Path(path)
    if path.suffix.lower() != '.pdf':
        raise StatementError('Сейчас поддерживаются только PDF-выписки поддерживаемых банков.')
    return read_statement_bytes(path.read_bytes(), suffix=path.suffix)


def read_statement_bytes(source, *, suffix='.pdf'):
    if suffix.lower() != '.pdf':
        raise StatementError('Поддерживаются только PDF-выписки поддерживаемых банков.')
    try:
        if not source.startswith(b'%PDF-'):
            raise StatementError('Файл не является PDF.')
        reader = PdfReader(BytesIO(source), strict=True)
        if reader.is_encrypted:
            raise StatementError('PDF защищён паролем. Нужна незашифрованная выписка.')
        pages = [page.extract_text(extraction_mode='layout') if '/Contents' in page else ''
                 for page in reader.pages]
        if pages and alfa_account_pdf_v1.matches(pages[0]):
            for index,page in enumerate(reader.pages):
                if 'Страница ' not in pages[index]:
                    extra=alfa_account_pdf_v1.pagination_from_forms(page)
                    pages[index]+='\n'+extra
    except (PyPdfError, ValueError, TypeError, KeyError) as error:
        if isinstance(error, StatementError):
            raise
        raise StatementError('Не удалось прочитать структуру PDF.') from error
    if not pages or not all(page and page.strip() for page in pages):
        raise StatementError('В PDF нет читаемого текста на всех страницах. Сканы пока не поддерживаются.')
    for parser in (sber_credit_pdf_v1, sber_debit_pdf_v1, ozon_pdf_v1, vtb_card_pdf_v1, rshb_card_pdf_v1, tbank_card_pdf_v1, alfa_account_pdf_v1):
        if parser.matches(pages[0]):
            return replace(parser.parse_pages(pages), source_sha256=hashlib.sha256(source).hexdigest())
    raise StatementError('Банк или формат PDF не распознан. Импорт не выполнен.')
