"""VTB credit-card RUB layout: signed amounts, two dates, separate time lines."""
import re
from ..models import BankOperation, Statement, StatementError, bank_date

DATE = r'\d{2}\.\d{2}\.\d{4}'
MONEY = r'-?\d+\.\d{1,2}'
ROW = re.compile(rf'^\s*({DATE})\s+({DATE})\s+({MONEY}) RUB\s+({MONEY})\s+({MONEY}) RUB\s+(.+?)\s*$')
TIME = re.compile(r'^\s*(\d{2}:\d{2}:\d{2})\s*$')


def matches(first):
    return all(label in first for label in ('СКС', 'Информация о балансе карты',
                                            'Операции по карте', 'Дата обработки'))


def money(text):
    if not re.fullmatch(MONEY,text):
        raise StatementError('ВТБ: неверная сумма.')
    whole,fraction=text.lstrip('-').split('.')
    return (-1 if text.startswith('-') else 1)*(int(whole)*100+int(fraction.ljust(2,'0')))


def single(pattern,text):
    values=list(re.finditer(pattern,text,re.M))
    if len(values)!=1:
        raise StatementError('ВТБ: отсутствует или повторяется обязательное поле.')
    return values[0]


def parse_pages(pages):
    pages=[page.replace('\xa0',' ').replace('\u202f',' ').replace('−','-') for page in pages]
    if not pages or not matches(pages[0]) or 'Банк ВТБ (ПАО)' not in pages[-1]:
        raise StatementError('ВТБ: неизвестный или неполный формат документа.')
    first=pages[0]
    account=single(r'^\s*СКС\s+([0-9]{20})\s*$',first).group(1)
    period=single(rf'Период выписки\s+({DATE})\s*-\s*({DATE})',first)
    start,end=map(bank_date,period.groups())
    if start>end:
        raise StatementError('ВТБ: неверный период.')
    opening=money(single(rf'Баланс на начало периода\s+({MONEY}) RUB',first).group(1))
    closing=money(single(rf'Баланс на конец периода\s+({MONEY}) RUB',first).group(1))
    incoming=money(single(rf'Поступления\s+({MONEY}) RUB',first).group(1))
    outgoing=money(single(rf'Расходные операции\s+({MONEY}) RUB',first).group(1))
    commission=money(single(rf'Комиссия банка\s+({MONEY}) RUB',first).group(1))
    if commission!=0:
        raise StatementError('ВТБ: ненулевая комиссия пока не поддерживается; запись запрещена.')
    if incoming<0 or outgoing<0:
        raise StatementError('ВТБ: отрицательные итоги оборотов.')
    operations,current=[],None
    ended=False

    def finish():
        nonlocal current
        if current is None:
            return
        if current['time'] is None:
            raise StatementError('ВТБ: отсутствует время операции.')
        operations.append(BankOperation(
            operation_date=current['date'],processing_date=current['processed'],
            description=' '.join(' '.join(current['parts']).split()),
            amount_kopecks=abs(current['amount']),direction='IN' if current['amount']>0 else 'OUT',
            balance_after_kopecks=None,page=current['page'],line=current['line'],
            operation_time=current['time'],authorization_code='',
            description_lines=tuple(current['parts']),operation_time_zone=None))
        current=None

    for number,page in enumerate(pages,1):
        lines=page.splitlines()
        nonempty=[i for i,line in enumerate(lines) if line.strip()]
        if not nonempty or lines[nonempty[-1]].strip()!=str(number):
            raise StatementError('ВТБ: нарушена нумерация страниц.')
        lines=lines[:nonempty[-1]]
        headers=[i for i,line in enumerate(lines) if all(label in line for label in
                 ('Дата и время','Дата обработки','Сумма операции в','Сумма в валюте','Комиссия','Описание операции'))]
        if ended:
            if (headers or not page.lstrip().startswith('Отчет по овердрафту')
                    or any(ROW.fullmatch(line) for line in lines)):
                raise StatementError('ВТБ: неизвестная страница после таблицы операций.')
            continue
        if len(headers)!=1:
            raise StatementError('ВТБ: неизвестный заголовок таблицы.')
        if number>1 and any(line.strip() for line in lines[:headers[0]]):
            raise StatementError('ВТБ: неизвестное содержимое перед таблицей.')
        body=list(enumerate(lines[headers[0]+1:],headers[0]+2))
        row_matches=[ROW.fullmatch(line) for _,line in body if ROW.fullmatch(line)]
        if not row_matches:
            raise StatementError('ВТБ: страница без распознаваемых операций.')
        description_column=min(row.start(6) for row in row_matches)
        seen_row=False
        for index,line in body:
            stripped=line.strip()
            if not stripped:
                continue
            if not seen_row and re.fullmatch(r'операции\s+банком\s+валюте операции\s+карты',stripped):
                continue
            if stripped=='Отчет по кредитной карте':
                finish()
                if any(ROW.fullmatch(value) for value in lines[index:]):
                    raise StatementError('ВТБ: операции после окончания таблицы.')
                ended=True
                break
            match=ROW.fullmatch(line)
            if match:
                finish();seen_row=True
                day,processed,amount,card_amount,fee,description=match.groups()
                value=money(amount)
                if money(fee)!=0:
                    raise StatementError('ВТБ: ненулевая комиссия операции пока не поддерживается.')
                if value==0 or abs(value)!=money(card_amount):
                    raise StatementError('ВТБ: нулевая сумма или валютная конвертация не поддерживается.')
                day,processed=bank_date(day),bank_date(processed)
                if not start<=processed<=end:
                    raise StatementError('ВТБ: дата обработки вне периода.')
                current=dict(date=day,processed=processed,amount=value,parts=[description],
                             page=number,line=index,time=None)
                continue
            time=TIME.fullmatch(line)
            if time:
                if current is None or current['time'] is not None:
                    raise StatementError('ВТБ: лишняя строка времени.')
                h,m,s=map(int,time[1].split(':'))
                if h>23 or m>59 or s>59:
                    raise StatementError('ВТБ: неверное время.')
                current['time']=time[1]
                continue
            if current and not line[:description_column].strip() and line[description_column:].strip():
                current['parts'].append(stripped)
                continue
            raise StatementError(f'ВТБ: страница {number}, строка {index}: неизвестная строка; запись запрещена.')
        finish()
    if not ended or not operations:
        raise StatementError('ВТБ: не найдено окончание таблицы.')
    actual_in=sum(row.amount_kopecks for row in operations if row.direction=='IN')
    actual_out=sum(row.amount_kopecks for row in operations if row.direction=='OUT')
    if (actual_in,actual_out)!=(incoming,outgoing):
        raise StatementError('ВТБ: суммы строк не совпадают с итогами банка; запись запрещена.')
    if opening+incoming-outgoing!=closing:
        raise StatementError('ВТБ: остатки не сходятся с оборотами; запись запрещена.')
    warnings=['Выписка не содержит остатков после отдельных операций и явного часового пояса. '
              'Время сохранено как в PDF; остатки строк не вычислялись.']
    if any(not start<=row.operation_date<=end for row in operations):
        warnings.append('Есть даты совершения вне периода; даты обработки сохранены отдельно, строки не отфильтрованы.')
    return Statement('ВТБ',account,'vtb_card_pdf','1',start,end,opening,closing,incoming,outgoing,
                     tuple(operations),tuple(warnings))
