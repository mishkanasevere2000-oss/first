"""Alfa RUR account layout; posting dates, nonunique operation codes, card MCC."""
from copy import copy
from datetime import date
import re
from pypdf.generic import NameObject
from ..models import BankOperation,Statement,StatementError,bank_date,kopecks,MONEY

DATE=r'\d{2}\.\d{2}\.\d{4}'
ROW=re.compile(rf'^\s*({DATE})\s+([A-Z0-9_]+(?:#[A-Z0-9_]+)?)\s+(.+?)\s{{2,}}({MONEY}) RUR\s*$')


def matches(first):
    return all(label in first for label in ('Выписка по счету','Код операции',
        'Дата проводки','Входящий остаток','Исходящий остаток','АЛЬФА-БАНК'))


def pagination_from_forms(page):
    """Read only invoked forms that inherit fonts from the page, without mutation."""
    resources=page['/Resources']
    objects=resources.get('/XObject',{})
    result=[]
    for args,operator in page.get_contents().operations:
        if operator!=b'Do':continue
        form=objects[args[0]].get_object()
        if form.get('/Subtype')!='/Form' or '/Resources' in form:continue
        inherited=copy(form)
        inherited[NameObject('/Resources')]=resources
        text=page.extract_xform_text(inherited).strip()
        if re.fullmatch(r'Страница \d+ из \d+',text):result.append(text)
    return '\n'.join(result)


def single(pattern,text):
    values=list(re.finditer(pattern,text,re.M))
    if len(values)!=1:
        raise StatementError('Альфа-Банк: отсутствует или повторяется обязательное поле.')
    return values[0]


def card_facts(description,posting):
    match=single(r'Операция по карте: ([0-9]{6}\+{6}[0-9]{4}), на сумму: (\d+\.\d{2}) RUR, '
        r'дата совершения операции: (\d{2})\.(\d{2})\.(\d{2}), место совершения операции:',description)
    card,amount,day,month,year=match.groups()
    # A two-digit year is accepted only in the posting year or preceding year;
    # do not silently assign an arbitrary century to unsupported historical data.
    posting_day=date.fromisoformat(posting)
    years=[y for y in (posting_day.year,posting_day.year-1) if y%100==int(year)]
    if len(years)!=1:
        raise StatementError('Альфа-Банк: неоднозначный год даты покупки.')
    try:operation_day=date(years[0],int(month),int(day))
    except ValueError as error:raise StatementError('Альфа-Банк: неверная дата покупки.') from error
    if operation_day>posting_day:
        raise StatementError('Альфа-Банк: дата покупки позже проводки.')
    mcc=single(r'\bMCC([0-9]{4})\b',description).group(1)
    return operation_day.isoformat(),card,kopecks(amount.replace('.',',')),mcc


def parse_pages(pages):
    pages=[p.replace('\xa0',' ').replace('\u202f',' ').replace('−','-') for p in pages]
    if not pages or not matches(pages[0]):
        raise StatementError('Формат не соответствует alfa_account_pdf v1.')
    first=pages[0]
    account=single(r'Номер счета\s+([0-9]{20})\b',first).group(1)
    single(r'Валюта счета\s+RUR\b',first)
    period=single(rf'За период с ({DATE}) по ({DATE})',first)
    start,end=map(bank_date,period.groups())
    if start>end:raise StatementError('Альфа-Банк: неверный период.')
    def total(label):return kopecks(single(rf'{label}\s+({MONEY}) RUR',first).group(1))
    opening,closing=total('Входящий остаток'),total('Исходящий остаток')
    incoming,outgoing=total('Поступления'),total('Расходы')
    if incoming<0 or outgoing<0:raise StatementError('Альфа-Банк: отрицательные обороты.')
    operations,current=[],None

    def finish():
        nonlocal current
        if current is None:return
        description=' '.join(' '.join(current['parts']).split())
        day=current['posting'];card=mcc=None;basis='POSTING_DATE'
        if current['code'].startswith('CRD_'):
            day,card,amount,mcc=card_facts(description,current['posting'])
            if amount!=abs(current['amount']):
                raise StatementError('Альфа-Банк: сумма карточного описания не совпадает с проводкой.')
            basis='DESCRIPTION'
        operations.append(BankOperation(operation_date=day,processing_date=current['posting'],
            description=description,amount_kopecks=abs(current['amount']),
            direction='IN' if current['amount']>0 else 'OUT',balance_after_kopecks=None,
            page=current['page'],line=current['line'],operation_time=None,authorization_code='',
            description_lines=tuple(current['parts']),mcc=mcc,card_number_mask=card,
            bank_operation_code=current['code'],operation_date_source=basis))
        current=None

    for number,page in enumerate(pages,1):
        pagination=single(r'Страница (\d+) из (\d+)',page)
        if tuple(map(int,pagination.groups()))!=(number,len(pages)):
            raise StatementError('Альфа-Банк: неполный документ или неверная нумерация.')
        if '(подпись сотрудника АО «АЛЬФА-БАНК»)' not in page:
            raise StatementError('Альфа-Банк: отсутствует банковский колонтитул.')
        lines=page.splitlines()
        headers=[i for i,line in enumerate(lines) if all(label in line for label in
                 ('Дата проводки','Код операции','Описание','Сумма'))]
        if len(headers)!=1:raise StatementError('Альфа-Банк: неизвестный заголовок таблицы.')
        header=headers[0]
        if number>1 and any(line.strip() for line in lines[:header]):
            raise StatementError('Альфа-Банк: неизвестное содержимое перед таблицей.')
        sub=next((i for i in range(header+1,len(lines)) if lines[i].strip()),None)
        if sub is None or lines[sub].strip()!='в валюте счета':
            raise StatementError('Альфа-Банк: неизвестный подзаголовок суммы.')
        signature=[i for i,line in enumerate(lines) if line.strip()=='Уполномоченное лицо']
        if len(signature)!=1:raise StatementError('Альфа-Банк: отсутствует окончание таблицы.')
        signer=next((i for i in range(signature[0]-1,sub,-1) if lines[i].strip()),None)
        if signer is None or not re.fullmatch(r'(?:[А-ЯЁ]\.\s*){2}[А-ЯЁа-яё-]+',lines[signer].strip()):
            raise StatementError('Альфа-Банк: неизвестное окончание таблицы.')
        desc_col=lines[header].index('Описание');amount_col=lines[header].index('Сумма')
        seen=False
        for index in range(sub+1,signer):
            line=lines[index]
            if not line.strip():continue
            row=ROW.fullmatch(line)
            if row:
                finish();seen=True
                posting,code,description,amount=row.groups();posting=bank_date(posting)
                if not start<=posting<=end:raise StatementError('Альфа-Банк: проводка вне периода.')
                amount=kopecks(amount)
                if not amount:raise StatementError('Альфа-Банк: нулевая сумма не поддерживается.')
                current=dict(posting=posting,code=code,amount=amount,parts=[description],page=number,line=index+1)
                continue
            if current and not line[:desc_col].strip() and not line[amount_col:].strip() and line[desc_col:amount_col].strip():
                current['parts'].append(line.strip());continue
            raise StatementError(f'Альфа-Банк: страница {number}, строка {index+1}: неизвестная строка.')
        finish()
        if not seen:raise StatementError('Альфа-Банк: страница без распознаваемых операций.')
    actual_in=sum(o.amount_kopecks for o in operations if o.direction=='IN')
    actual_out=sum(o.amount_kopecks for o in operations if o.direction=='OUT')
    if (actual_in,actual_out)!=(incoming,outgoing):
        raise StatementError('Альфа-Банк: суммы строк не совпадают с итогами банка; запись запрещена.')
    if opening+incoming-outgoing!=closing:
        raise StatementError('Альфа-Банк: остатки не сходятся с оборотами; запись запрещена.')
    warnings=['Для операций без отдельной даты совершения сохранена дата проводки с соответствующей пометкой в исходных данных. '
              'Время и остатки после отдельных операций отсутствуют: не вычислялись.']
    if any(not start<=o.operation_date<=end for o in operations):
        warnings.append('Есть даты покупок вне периода; даты проводки сохранены отдельно, строки не отфильтрованы.')
    return Statement('Альфа-Банк',account,'alfa_account_pdf','1',start,end,opening,closing,
                     incoming,outgoing,tuple(operations),tuple(warnings))
