"""RSHB RUB card statement without bank turnover totals or printed page numbers."""
import re
from ..models import BankOperation,Statement,StatementError,bank_date

DATE=r'\d{2}\.\d{2}\.\d{4}'
MONEY=r'-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}'
ROW=re.compile(rf'^\s*({DATE})\s+({DATE})\s+({MONEY}) ₽\s+({MONEY}) ₽\s+({MONEY}) ₽\s+(.+?)\s*$')
TIME=re.compile(r'^\s*(\d{2}:\d{2})(?:\s+(.*?))?\s*$')


def matches(first):
    return all(label in first for label in ('Дополнительный офис №3349/',
        'Выписка по карточному счёту','Выписка по счёту:', 'Валюта счёта:',
        'Сумма входящего остатка:', 'Сумма исходящего остатка:', 'Операции за период с'))


def money(text):
    if not re.fullmatch(MONEY,text):
        raise StatementError('РСХБ: неверная сумма.')
    whole,fraction=text.lstrip('-').replace(',','').split('.')
    return (-1 if text.startswith('-') else 1)*(int(whole)*100+int(fraction))


def single(pattern,text):
    found=list(re.finditer(pattern,text,re.M))
    if len(found)!=1:
        raise StatementError('РСХБ: отсутствует или повторяется обязательное поле.')
    return found[0]


def parse_pages(pages):
    pages=[p.replace('\xa0',' ').replace('\u202f',' ').replace('−','-') for p in pages]
    if not pages or not matches(pages[0]):
        raise StatementError('Формат не соответствует rshb_card_pdf v1.')
    first=pages[0]
    account=single(rf'^\s*([0-9]{{20}})\s+Российский рубль\s+({DATE})\s*$',first).group(1)
    period=single(rf'Операции за период с ({DATE}) по ({DATE})',first)
    start,end=map(bank_date,period.groups())
    if start>end:
        raise StatementError('РСХБ: неверный период.')
    balances=single(rf'Сумма входящего остатка:\s+Сумма исходящего остатка:\s*\n\s*({MONEY})\s+({MONEY})\s*$',first)
    opening,closing=map(money,balances.groups())
    operations,current=[],None

    def finish():
        nonlocal current
        if current is None:
            return
        if current['time'] is None:
            raise StatementError('РСХБ: отсутствует время операции.')
        operations.append(BankOperation(operation_date=current['date'],processing_date=current['processed'],
            description=' '.join(' '.join(current['parts']).split()),
            amount_kopecks=abs(current['amount']),direction='IN' if current['amount']>0 else 'OUT',
            balance_after_kopecks=None,page=current['page'],line=current['line'],
            operation_time=current['time'],authorization_code='',
            description_lines=tuple(current['parts']),card_number_mask=current['card']))
        current=None

    for number,page in enumerate(pages,1):
        lines=page.splitlines()
        headings=[i for i,line in enumerate(lines) if all(label in line for label in
                  ('операции','обработки','валюте счета','Комиссия','Содержание операции','№ карты'))]
        if len(headings)!=1:
            raise StatementError('РСХБ: неизвестный заголовок таблицы.')
        header=lines[headings[0]]
        previous=next((line.strip() for line in reversed(lines[:headings[0]]) if line.strip()),'')
        if not re.fullmatch(r'Дата и время\s+Дата\s+Сумма\s+Сумма в',previous):
            raise StatementError('РСХБ: неизвестные заголовки дат и сумм.')
        if number>1:
            preamble=[line.strip() for line in lines[:headings[0]] if line.strip()]
            if preamble!=[previous]:
                raise StatementError('РСХБ: неизвестное содержимое перед таблицей.')
        desc_col,card_col=header.index('Содержание операции'),header.index('№ карты')
        seen=False
        for index in range(headings[0]+1,len(lines)):
            line=lines[index]
            if not line.strip():
                continue
            row=ROW.fullmatch(line)
            if row:
                finish();seen=True
                day,processed,amount,account_amount,fee,description=row.groups()
                value=money(amount)
                if value==0 or value!=money(account_amount):
                    raise StatementError('РСХБ: нулевая сумма или конвертация не поддерживается.')
                if money(fee)!=0:
                    raise StatementError('РСХБ: ненулевая комиссия пока не поддерживается.')
                day,processed=bank_date(day),bank_date(processed)
                if not start<=processed<=end:
                    raise StatementError('РСХБ: дата обработки вне периода.')
                card=line[card_col:].strip()
                if card and not re.fullmatch(r'\*{2}[0-9]{4}',card):
                    raise StatementError('РСХБ: неизвестная колонка номера карты.')
                description=line[row.start(6):card_col].strip()
                if not description:
                    raise StatementError('РСХБ: отсутствует описание.')
                current=dict(date=day,processed=processed,amount=value,time=None,
                             page=number,line=index+1,parts=[description],card=card or None)
                continue
            time=TIME.fullmatch(line)
            if time:
                if current is None or current['time'] is not None:
                    raise StatementError('РСХБ: лишняя строка времени.')
                h,m=map(int,time[1].split(':'))
                if h>23 or m>59:
                    raise StatementError('РСХБ: неверное время.')
                current['time']=time[1]
                # PDF extraction can put the next description fragment on the time line.
                if time[2]:
                    if line[:desc_col].strip()!=time[1] or line[card_col:].strip():
                        raise StatementError('РСХБ: неизвестная строка времени/описания.')
                    current['parts'].append(line[desc_col:card_col].strip())
                continue
            if current and not line[:desc_col].strip() and not line[card_col:].strip() and line[desc_col:card_col].strip():
                current['parts'].append(line.strip())
                continue
            raise StatementError(f'РСХБ: страница {number}, строка {index+1}: неизвестная строка; запись запрещена.')
        finish()
        if not seen:
            raise StatementError('РСХБ: страница без распознаваемых операций.')
    incoming=sum(row.amount_kopecks for row in operations if row.direction=='IN')
    outgoing=sum(row.amount_kopecks for row in operations if row.direction=='OUT')
    if opening+incoming-outgoing!=closing:
        raise StatementError('РСХБ: остатки не сходятся с суммами строк; запись запрещена.')
    warnings=['Банк не указал отдельные итоги пополнений/списаний: суммы рассчитаны по строкам. '
              'Проверено изменение остатка; полноту PDF по итогам банка и номерам страниц подтвердить нельзя.',
              'Остатки после отдельных операций и часовой пояс не указаны. Время сохранено как в PDF.']
    if any(not start<=row.operation_date<=end for row in operations):
        warnings.append('Есть даты совершения вне периода; даты обработки сохранены отдельно, строки не отфильтрованы.')
    return Statement('Россельхозбанк',account,'rshb_card_pdf','1',start,end,opening,closing,
        incoming,outgoing,tuple(operations),tuple(warnings),bank_turnover_totals_present=False)
