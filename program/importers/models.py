"""Banking facts only. Direction is not an analytical income/expense type."""
from dataclasses import dataclass
from datetime import date
import re


class StatementError(ValueError):
    """Unsupported, incomplete or inconsistent source statement."""


MONEY = r'[+-]?(?:\d{1,3}(?: \d{3})+|\d+),\d{2}'


def kopecks(text):
    value = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('−', '-').strip()
    if not re.fullmatch(MONEY, value):
        raise StatementError('Неверная сумма: требуется число с двумя знаками после запятой.')
    negative = value.startswith('-')
    whole, fraction = value.lstrip('+-').replace(' ', '').split(',')
    result = int(whole) * 100 + int(fraction)
    return -result if negative else result


def bank_date(text):
    try:
        day, month, year = map(int, text.split('.'))
        return date(year, month, day).isoformat()
    except (ValueError, TypeError) as error:
        raise StatementError('Неверная дата в банковской выписке.') from error


@dataclass(frozen=True)
class BankOperation:
    operation_date: str
    processing_date: str
    description: str
    amount_kopecks: int
    direction: str
    balance_after_kopecks: int
    page: int
    line: int
    operation_time: str
    authorization_code: str
    description_lines: tuple[str, ...]


@dataclass(frozen=True)
class Statement:
    bank: str
    account_id: str
    parser_name: str
    parser_version: str
    period_start: str
    period_end: str
    opening_balance_kopecks: int
    closing_balance_kopecks: int
    incoming_kopecks: int
    outgoing_kopecks: int
    operations: tuple[BankOperation, ...]
    warnings: tuple[str, ...]

    @property
    def incoming_count(self):
        return sum(row.direction == 'IN' for row in self.operations)

    @property
    def outgoing_count(self):
        return sum(row.direction == 'OUT' for row in self.operations)
