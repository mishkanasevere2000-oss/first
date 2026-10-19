"""One text PDF layout: Sber credit-card account statement, no categorization."""
import re

from ..models import BankOperation, MONEY, Statement, StatementError, bank_date, kopecks

TITLE = 'Выписка по счёту кредитной карты'
DATE = r'\d{2}\.\d{2}\.\d{4}'
START = re.compile(rf'^\s*({DATE})\s+(\d{{2}}:\d{{2}})\b')
ROW = re.compile(rf'^\s*({DATE})\s+(\d{{2}}:\d{{2}})\s+(.+?)\s{{2,}}({MONEY})\s{{2,}}({MONEY})\s*$')
PROCESSING = re.compile(rf'^\s*({DATE})\s+(\d{{6}})\s+(.+?)\s*$')


def clean(text):
    return text.replace('\xa0', ' ').replace('\u202f', ' ').replace('−', '-')


def matches(first_page):
    return (TITLE in first_page and 'www.sberbank.ru' in first_page
            and 'Заказано в СберБанк Онлайн' in first_page)


def one_match(pattern, text, message):
    values = list(re.finditer(pattern, text, re.M))
    if len(values) != 1:
        raise StatementError(message)
    return values[0]


def parse_pages(pages):
    if not pages or not matches(pages[0]):
        raise StatementError('Формат не соответствует sber_credit_pdf_v1.')
    pages = [clean(page) for page in pages]
    first = pages[0]
    if not re.search(r'Валюта\s+Российский рубль\b', first):
        raise StatementError('Поддерживается только рублёвая выписка этого формата.')
    period = one_match(rf'За период ({DATE}) [—–-] ({DATE})', first, 'Не удалось однозначно прочитать период.')
    period_start, period_end = map(bank_date, period.groups())
    if period_start > period_end:
        raise StatementError('Начало периода позже его окончания.')
    account = one_match(r'Номер сч[её]та\s+([\d ]+?)(?=\s{2,}[^\d\s]|\s*$)', first,
                        'Не удалось однозначно прочитать номер счёта.').group(1).replace(' ', '')
    if len(account) != 20 or not account.isascii() or not account.isdigit():
        raise StatementError('Номер счёта должен содержать 20 цифр.')
    incoming = kopecks(one_match(rf'\bПополнение\s+({MONEY})\s*$', first, 'Не прочитан итог пополнений.').group(1))
    outgoing = kopecks(one_match(rf'\bСписание\s+({MONEY})\s*$', first, 'Не прочитан итог списаний.').group(1))
    balances = list(re.finditer(rf'Остаток на ({DATE})\s+({MONEY})\s*$', first, re.M))
    if len(balances) != 2 or [bank_date(row.group(1)) for row in balances] != [period_start, period_end]:
        raise StatementError('Не прочитаны начальный и конечный остатки.')
    opening, closing = [kopecks(row.group(2)) for row in balances]
    if incoming < 0 or outgoing < 0:
        raise StatementError('Отрицательные итоги оборотов не поддерживаются.')
    operations = []
    current = None

    def finish():
        nonlocal current
        if current is None:
            return
        if current['processing_date'] is None or not current['parts']:
            raise StatementError(f"Страница {current['page']}, строка {current['line']}: нет даты обработки или описания.")
        values = {key: value for key, value in current.items() if key != 'parts'}
        values['description'] = ' '.join(' '.join(current['parts']).split())
        values['description_lines'] = tuple(current['parts'])
        operations.append(BankOperation(**values, operation_time_zone='Europe/Moscow'))
        current = None

    for page_number, page in enumerate(pages, start=1):
        if page_number > 1:
            pagination = one_match(r'Страница (\d+) из (\d+)', page, f'Страница {page_number}: не прочитана нумерация.')
            if tuple(map(int, pagination.groups())) != (page_number, len(pages)):
                raise StatementError('Неполный документ или нарушен порядок страниц.')
            if TITLE not in page:
                raise StatementError(f'Страница {page_number}: другой тип документа.')
        lines = page.splitlines()
        headers = [(number, line) for number, line in enumerate(lines)
                   if 'ДАТА ОПЕРАЦИИ (МСК)' in line and 'КАТЕГОРИЯ' in line
                   and 'СУММА В РУБЛЯХ' in line and 'ОСТАТОК СРЕДСТВ' in line]
        if len(headers) != 1:
            raise StatementError(f'Страница {page_number}: неизвестные заголовки таблицы.')
        header_number, header = headers[0]
        description_column = header.index('КАТЕГОРИЯ')
        amount_column = header.index('СУММА В РУБЛЯХ')
        ended = False
        for index in range(header_number + 1, len(lines)):
            line = lines[index]
            stripped = line.strip()
            if not stripped or stripped.startswith(('Дата обработки', 'и код авторизации')):
                continue
            if stripped.startswith(('Продолжение на следующей странице', 'Дата формирования документа')):
                ended = True
                break
            if stripped == '*' and page_number == len(pages):
                following = next((value.strip() for value in lines[index+1:] if value.strip()), '')
                if following.startswith('Дата формирования документа'):
                    continue
            match = ROW.fullmatch(line)
            if match:
                finish()
                operation_date, time, _bank_category, amount, balance = match.groups()
                hours, minutes = map(int, time.split(':'))
                if hours > 23 or minutes > 59:
                    raise StatementError(f'Страница {page_number}, строка {index+1}: неверное время.')
                signed_amount = kopecks(amount)
                current = dict(operation_date=bank_date(operation_date), processing_date=None,
                               parts=[], amount_kopecks=abs(signed_amount),
                               direction='IN' if amount.startswith('+') else 'OUT',
                               balance_after_kopecks=kopecks(balance), page=page_number, line=index+1,
                               operation_time=time, authorization_code='')
                continue
            if START.match(line):
                raise StatementError(f'Страница {page_number}, строка {index+1}: не прочитаны сумма или остаток.')
            processing = PROCESSING.fullmatch(line)
            if processing:
                if current is None or current['processing_date'] is not None:
                    raise StatementError(f'Страница {page_number}, строка {index+1}: лишняя дата обработки.')
                processing_date, code, description = processing.groups()
                current.update(processing_date=bank_date(processing_date), authorization_code=code)
                current['parts'].append(description)
                continue
            # A continuation must remain in the description column. Never skip a
            # broken date/amount line or an unrecognized piece of table content.
            if (current and current['processing_date'] and not line[:description_column].strip()
                    and line[description_column:amount_column].strip() and not line[amount_column:].strip()):
                current['parts'].append(stripped)
                continue
            raise StatementError(f'Страница {page_number}, строка {index+1}: неизвестная строка таблицы; импорт остановлен.')
        if not ended:
            raise StatementError(f'Страница {page_number}: не найдено окончание таблицы.')
    finish()
    if not operations:
        raise StatementError('Таблица операций пуста или не прочитана.')
    actual_in = sum(row.amount_kopecks for row in operations if row.direction == 'IN')
    actual_out = sum(row.amount_kopecks for row in operations if row.direction == 'OUT')
    if (actual_in, actual_out) != (incoming, outgoing):
        raise StatementError('Суммы прочитанных строк не совпадают с итогами банка; запись в базу запрещена.')
    warnings = []
    if opening + incoming - outgoing != closing:
        warnings.append('Конечный остаток в шапке не сходится с начальным остатком и оборотами. '
                        'Обороты строк сверены; исходные остатки сохранены без исправлений.')
    if any(not period_start <= row.operation_date <= period_end for row in operations):
        warnings.append('Есть операции с датой совершения вне периода шапки. '
                        'Дата совершения сохранена отдельно от даты обработки; строки не отфильтрованы.')
    return Statement('Сбербанк', account, 'sber_credit_pdf', '1', period_start, period_end,
                     opening, closing, incoming, outgoing, tuple(operations), tuple(warnings))
