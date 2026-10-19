"""Stable banking identities with occurrence counting, never category inference."""
import hashlib
import json


def identity(record):
    bank = record['bank']
    account = record['account_id']
    bank_id = record.get('bank_transaction_id')
    if bank_id:
        if not isinstance(bank_id, str) or not bank_id.strip():
            raise ValueError('Банковский ID должен быть непустым текстом.')
        kind, facts = 'BANK_ID', bank_id.strip()
    else:
        data = record.get('source_data') or {}
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data,dict):
            raise ValueError('Исходные данные операции должны быть JSON-объектом.')
        kind = 'FINGERPRINT'
        facts = [record['operation_date'], record.get('processing_date'),
                 record['amount_kopecks'], record['direction'],
                 ' '.join(record['description'].split()), record.get('mcc'),
                 data.get('operation_time_moscow', data.get('operation_time_local')), data.get('authorization_code')]
    digest = hashlib.sha256(json.dumps(facts, ensure_ascii=False, separators=(',', ':'),
                                      sort_keys=True, allow_nan=False).encode()).hexdigest()
    return bank, account, kind, digest


def attach_identity(connection, record):
    bank, account, kind, key = identity(record)
    occurrence = 1
    if kind == 'FINGERPRINT':
        occurrence += connection.execute(
            'SELECT coalesce(max(occurrence),0) FROM operation_identities '
            'WHERE bank=? AND account_id=? AND kind=? AND identity_key=?',
            (bank, account, kind, key)).fetchone()[0]
    connection.execute('INSERT INTO operation_identities VALUES (?,?,?,?,?,?)',
                       (record['id'], bank, account, kind, key, occurrence))


def match_occurrences(connection, records):
    """Preserve multiplicity; balances help assign equal events, not define identity."""
    groups = {}
    for index, record in enumerate(records):
        groups.setdefault(identity(record), []).append(index)
    result = [None] * len(records)
    for key, indices in groups.items():
        if key[2] == 'BANK_ID' and len(indices) > 1:
            raise ValueError('Один банковский ID повторён внутри файла. Нужна проверка источника.')
        existing = [dict(row) for row in connection.execute(
            'SELECT o.*,i.occurrence FROM operation_identities i JOIN operations o ON o.id=i.operation_id '
            'WHERE i.bank=? AND i.account_id=? AND i.kind=? AND i.identity_key=? ORDER BY i.occurrence', key)]
        if key[2] == 'BANK_ID':
            if existing:
                for field in ('operation_date','amount_kopecks','direction','description'):
                    if existing[0][field] != records[indices[0]][field]:
                        raise ValueError('Банковский ID совпал, но данные операции изменились. Нужна проверка.')
                result[indices[0]] = existing[0]['id']
            continue
        available = {row['id']: row for row in existing}
        # First reserve exact balance matches across the whole group; this keeps
        # an existing manually reviewed event attached to its original row when possible.
        for index in indices:
            balance = records[index].get('balance_after_kopecks')
            if balance is None:
                continue
            match = next((identifier for identifier,row in available.items()
                          if row['balance_after_kopecks'] == balance),None)
            if match is not None:
                result[index] = match
                del available[match]
        for index in indices:
            if result[index] is None and available:
                identifier = next(iter(available))
                result[index] = identifier
                del available[identifier]
    return result
