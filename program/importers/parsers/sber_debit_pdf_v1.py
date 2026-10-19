"""Sber debit-card RUB layout using the shared, strictly validated Sber rows."""
from . import sber_credit_pdf_v1 as shared

TITLE = 'Выписка по счёту дебетовой карты'


def matches(first_page):
    return shared.matches(first_page, title=TITLE)


def parse_pages(pages):
    return shared.parse_sber_pages(pages, title=TITLE,
                                   amount_header='СУММА В ВАЛЮТЕ СЧЁТА',
                                   parser_name='sber_debit_pdf', debit=True)
