"""Ozon Bank RUB movement certificate: strict rows, wrapped document IDs, totals."""
import re
from ..models import BankOperation, Statement, StatementError, bank_date

DATE = r'\d{2}\.\d{2}\.\d{4}'
MONEY = r'\d+(?: \d{3})*\.\d{2}'
SIGNED = rf'[+-]\s*{MONEY}'
ROW = re.compile(rf'^\s*({DATE})\s+(\d{{2}}:\d{{2}}:\d{{2}})\s+(\d+)\s{{2,}}(.+?)\s{{2,}}({SIGNED})\s*₽\s+({SIGNED})\s*₽\s*$')


def matches(first):
    return 'Справка о движении средств' in first and 'ОЗОН Банк' in first


def money(text):
    value = text.replace(' ', '')
    if not re.fullmatch(r'[+-]?\d+\.\d{2}', value):
        raise StatementError('Ozon: неверная сумма.')
    whole, fraction = value.lstrip('+-').split('.')
    return (-1 if value.startswith('-') else 1) * (int(whole)*100+int(fraction))


def single(pattern, text):
    found = list(re.finditer(pattern, text, re.M))
    if len(found) != 1:
        raise StatementError('Ozon: отсутствует или повторяется обязательное поле выписки.')
    return found[0]


def parse_pages(pages):
    if not pages or not matches(pages[0]):
        raise StatementError('Формат не соответствует ozon_pdf_v1.')
    pages = [p.replace('\xa0',' ').replace('\u202f',' ').replace('−','-') for p in pages]
    first, last = pages[0], pages[-1]
    single(r'^Валюта:\s*РОССИЙСКИЙ РУБЛЬ\s*$', first)
    account = single(r'Номер лицевого сч[её]та:\s*№\s*([0-9]{20}),', first).group(1)
    period = single(rf'Период выписки:\s*({DATE})\s*[–—-]\s*({DATE})\s*$', first)
    start, end = map(bank_date, period.groups())
    if start > end:
        raise StatementError('Ozon: неверный период.')
    opening = money(single(rf'^Входящий остаток:\s*({MONEY})\s*₽\s*$', first).group(1))
    closing = money(single(rf'^Исходящий остаток:\s*({MONEY})\s*₽\s*$', last).group(1))
    incoming = money(single(rf'^Итого зачислений за период:\s*({MONEY})\s*₽\s*$', last).group(1))
    outgoing = money(single(rf'^Итого списаний за период:\s*({MONEY})\s*₽\s*$', last).group(1))
    operations, current = [], None

    def finish():
        nonlocal current
        if current is None:
            return
        operations.append(BankOperation(
            operation_date=current['date'], processing_date=None,
            description=' '.join(' '.join(current['parts']).split()),
            amount_kopecks=abs(current['amount']), direction='IN' if current['amount']>0 else 'OUT',
            balance_after_kopecks=None, page=current['page'], line=current['line'],
            operation_time=current['time'], authorization_code='',
            description_lines=tuple(current['parts']), bank_transaction_id=current['id'],
            operation_time_zone=None))
        current = None

    for number, page in enumerate(pages,1):
        lines = page.splitlines()
        nonempty = [i for i,line in enumerate(lines) if line.strip()]
        if not nonempty or lines[nonempty[-1]].strip() != str(number):
            raise StatementError('Ozon: нарушена нумерация страниц.')
        lines = lines[:nonempty[-1]]
        if number == 1:
            headers=[i for i,line in enumerate(lines) if 'Российские рубли' in line and 'Валюта' in line]
            if len(headers)!=1 or 'Назначение платежа' not in first or 'Документ' not in first:
                raise StatementError('Ozon: неизвестный заголовок таблицы.')
            lines_with_indices=list(enumerate(lines[headers[0]+1:],headers[0]+2))
        else:
            lines_with_indices=list(enumerate(lines,1))
        first_row=next((ROW.fullmatch(line) for _,line in lines_with_indices if ROW.fullmatch(line)),None)
        if first_row is None:
            raise StatementError('Ozon: страница без распознаваемых операций.')
        doc_col, desc_col = first_row.start(3), first_row.start(4)
        amount_col=min(ROW.fullmatch(line).start(5) for _,line in lines_with_indices if ROW.fullmatch(line))
        ended=False
        for index,line in lines_with_indices:
            if not line.strip():
                continue
            if line.strip().startswith('Итого зачислений за период:'):
                if number != len(pages):
                    raise StatementError('Ozon: итоги до последней страницы.')
                tail='\n'.join(lines[index:])
                if re.search(rf'{DATE}\s+\d{{2}}:',tail):
                    raise StatementError('Ozon: операции после итогов.')
                ended=True
                break
            row=ROW.fullmatch(line)
            if row:
                finish()
                day,time,document,description,amount,currency_amount=row.groups()
                h,m,s=map(int,time.split(':'))
                if h>23 or m>59 or s>59:
                    raise StatementError('Ozon: неверное время.')
                value=money(amount)
                if value==0 or value!=money(currency_amount):
                    raise StatementError('Ozon: нулевая сумма или разные суммы валютных колонок.')
                day=bank_date(day)
                if not start<=day<=end:
                    raise StatementError('Ozon: дата операции вне периода.')
                current=dict(date=day,time=time,id=document,parts=[description],amount=value,page=number,line=index)
                continue
            # The document column can contain the final digits of a wrapped ID.
            prefix=line[:doc_col].strip()
            document_tail=line[doc_col:desc_col].strip()
            description=line[desc_col:amount_col].strip()
            if (current and not prefix and not line[amount_col:].strip()
                    and (not document_tail or document_tail.isascii() and document_tail.isdigit())
                    and (document_tail or description)):
                current['id']+=document_tail
                if description:
                    current['parts'].append(description)
                continue
            raise StatementError(f'Ozon: страница {number}, строка {index}: неизвестная строка; импорт остановлен.')
        finish()
        if number==len(pages) and not ended:
            raise StatementError('Ozon: не найдено окончание таблицы.')
    ids=[o.bank_transaction_id for o in operations]
    if len(set(ids))!=len(ids):
        raise StatementError('Ozon: номер документа повторяется; нужна проверка источника.')
    actual_in=sum(o.amount_kopecks for o in operations if o.direction=='IN')
    actual_out=sum(o.amount_kopecks for o in operations if o.direction=='OUT')
    if (actual_in,actual_out)!=(incoming,outgoing):
        raise StatementError('Ozon: суммы строк не совпадают с итогами банка; запись запрещена.')
    if opening+incoming-outgoing!=closing:
        raise StatementError('Ozon: начальный остаток, обороты и конечный остаток не сходятся.')
    return Statement('ОЗОН Банк',account,'ozon_pdf','1',start,end,opening,closing,incoming,outgoing,
                     tuple(operations),('Дата обработки и остатки после отдельных операций в выписке отсутствуют. '
                                        'Часовой пояс времени операций не указан; значения сохранены как в PDF.',))
