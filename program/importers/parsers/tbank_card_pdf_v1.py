"""T-Bank RUB movement certificate: two signed amounts, two dates and times."""
import re
from ..models import BankOperation,Statement,StatementError,bank_date

DATE=r'\d{2}\.\d{2}\.\d{4}'
MONEY=r'(?:\d{1,3}(?: \d{3})+|\d+)\.\d{2}'
SUMMARY=MONEY.replace(r'\.', ',')
SIGNED=rf'[+-]{MONEY}'
ROW=re.compile(rf'^\s*({DATE})\s+({DATE})\s+({SIGNED}) ₽\s+({SIGNED}) ₽\s+(.+?)\s{{2,}}([0-9]{{4}}|—)\s*$')
TIME=re.compile(r'^\s*(\d{2}:\d{2})\s+(\d{2}:\d{2})(?:\s{2,}(.*?))?\s*$')


def matches(first):
    return all(label in first for label in ('Справка о движении средств','TBANK.RU',
                                            'Номер лицевого счета:','Движение средств за период с'))


def money(text):
    if not re.fullmatch(rf'[+-]?{MONEY}',text):
        raise StatementError('Т-Банк: неверная сумма.')
    whole,fraction=text.lstrip('+-').replace(' ','').split('.')
    return (-1 if text.startswith('-') else 1)*(int(whole)*100+int(fraction))


def single(pattern,text):
    values=list(re.finditer(pattern,text,re.M))
    if len(values)!=1:
        raise StatementError('Т-Банк: отсутствует или повторяется обязательное поле.')
    return values[0]


def parse_pages(pages):
    pages=[p.replace('\xa0',' ').replace('\u202f',' ').replace('−','-') for p in pages]
    if not pages or not matches(pages[0]):
        raise StatementError('Формат не соответствует tbank_card_pdf v1.')
    account=single(r'^\s*Номер лицевого счета:\s*([0-9]{20})\s*$',pages[0]).group(1)
    period=single(rf'Движение средств за период с ({DATE}) по ({DATE})',pages[0])
    start,end=map(bank_date,period.groups())
    if start>end:
        raise StatementError('Т-Банк: неверный период.')
    incoming=money(single(rf'^\s*Пополнения:\s+({SUMMARY}) ₽\s*$',pages[-1]).group(1).replace(',', '.'))
    outgoing=money(single(rf'^\s*Расходы:\s+({SUMMARY}) ₽\s*$',pages[-1]).group(1).replace(',', '.'))
    operations,current=[],None
    totals_seen=False

    def finish():
        nonlocal current
        if current is None:return
        if current['time'] is None or current['processed_time'] is None:
            raise StatementError('Т-Банк: отсутствует время операции или списания.')
        operations.append(BankOperation(operation_date=current['day'],processing_date=current['processed'],
            description=' '.join(' '.join(current['parts']).split()),
            amount_kopecks=abs(current['amount']),direction='IN' if current['amount']>0 else 'OUT',
            balance_after_kopecks=None,page=current['page'],line=current['line'],
            operation_time=current['time'],authorization_code='',description_lines=tuple(current['parts']),
            card_number_mask=current['card'],processing_time=current['processed_time']))
        current=None

    for number,page in enumerate(pages,1):
        lines=page.splitlines();nonempty=[i for i,line in enumerate(lines) if line.strip()]
        if not nonempty or lines[nonempty[-1]].strip()!=str(number):
            raise StatementError('Т-Банк: нарушена нумерация страниц.')
        footers=[i for i,line in enumerate(lines) if line.strip().startswith(
            'АО «ТБанк» универсальная лицензия Банка России № 2673')]
        if len(footers)!=1:
            raise StatementError('Т-Банк: отсутствует банковский колонтитул.')
        headers=[i for i,line in enumerate(lines[:footers[0]]) if all(label in line for label in
                 ('Дата и время','Дата','Сумма в валюте','Сумма операции','Описание','Номер'))]
        if len(headers)!=1:
            raise StatementError('Т-Банк: неизвестный заголовок таблицы.')
        header=headers[0]
        if number>1 and any(line.strip() for line in lines[:header]):
            raise StatementError('Т-Банк: неизвестное содержимое перед таблицей.')
        subheading=next((i for i in range(header+1,footers[0]) if lines[i].strip()),None)
        if subheading is None or not re.fullmatch(r'операции\s+списания\s+операции\s+в валюте карты\s+операции\s+карты',lines[subheading].strip()):
            raise StatementError('Т-Банк: неизвестные подзаголовки колонок.')
        body=list(enumerate(lines[subheading+1:footers[0]],subheading+2))
        row_matches=[ROW.fullmatch(line) for _,line in body if ROW.fullmatch(line)]
        if not row_matches:
            raise StatementError('Т-Банк: страница без распознаваемых операций.')
        desc_col=min(match.start(5) for match in row_matches)
        card_col=min(match.start(6) for match in row_matches)
        for index,line in body:
            stripped=line.strip()
            if not stripped:continue
            if stripped.startswith('Пополнения:'):
                finish()
                if number!=len(pages):
                    raise StatementError('Т-Банк: итоги до последней страницы.')
                tail='\n'.join(lines[index:footers[0]])
                # Do not silently discard malformed/foreign rows after totals.
                if re.search(rf'{DATE}\s+{DATE}',tail):
                    raise StatementError('Т-Банк: операции после итогов.')
                following=next((value.strip() for value in lines[index:footers[0]] if value.strip()),'')
                if not re.fullmatch(rf'Расходы:\s+{SUMMARY} ₽',following):
                    raise StatementError('Т-Банк: нарушен порядок итогов.')
                totals_seen=True
                break
            row=ROW.fullmatch(line)
            if row:
                finish()
                day,processed,amount,card_amount,description,card=row.groups()
                value=money(amount)
                if value==0 or value!=money(card_amount):
                    raise StatementError('Т-Банк: нулевая сумма или конвертация не поддерживается.')
                day,processed=bank_date(day),bank_date(processed)
                if not start<=processed<=end:
                    raise StatementError('Т-Банк: дата списания вне периода.')
                current=dict(day=day,processed=processed,amount=value,parts=[description],
                    card=None if card=='—' else card,time=None,processed_time=None,page=number,line=index)
                continue
            time=TIME.fullmatch(line)
            if time:
                if current is None or current['time'] is not None:
                    raise StatementError('Т-Банк: лишняя строка времени.')
                for value in time.groups()[:2]:
                    h,m=map(int,value.split(':'))
                    if h>23 or m>59:
                        raise StatementError('Т-Банк: неверное время.')
                current['time'],current['processed_time']=time[1],time[2]
                if time[3]:
                    if line[:desc_col].split()!=[time[1],time[2]] or line[card_col:].strip():
                        raise StatementError('Т-Банк: неизвестная строка времени/описания.')
                    current['parts'].append(time[3])
                continue
            if current and not line[:desc_col].strip() and not line[card_col:].strip() and line[desc_col:card_col].strip():
                current['parts'].append(stripped)
                continue
            raise StatementError(f'Т-Банк: страница {number}, строка {index}: неизвестная строка; запись запрещена.')
        finish()
    if not totals_seen or not operations:
        raise StatementError('Т-Банк: не найдено окончание таблицы.')
    actual_in=sum(row.amount_kopecks for row in operations if row.direction=='IN')
    actual_out=sum(row.amount_kopecks for row in operations if row.direction=='OUT')
    if (actual_in,actual_out)!=(incoming,outgoing):
        raise StatementError('Т-Банк: суммы строк не совпадают с итогами банка; запись запрещена.')
    warnings=['Начальный/конечный остатки и остатки после операций не указаны: сохранены как отсутствующие. '
              'Обороты сверены с итогами банка. Часовой пояс двух времён не указан; значения сохранены как в PDF.']
    if any(not start<=row.operation_date<=end for row in operations):
        warnings.append('Есть даты совершения вне периода; даты списания сохранены отдельно, строки не отфильтрованы.')
    return Statement('Т-Банк',account,'tbank_card_pdf','1',start,end,None,None,
                     incoming,outgoing,tuple(operations),tuple(warnings))
