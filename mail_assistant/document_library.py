"""A complete local document inventory, including files without AI results.

This view deliberately does not apply a payment-date filter: an unrecognised
document has no reliable payment date and must not disappear from the UI.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from mail_assistant.presentation import money_label
from mail_assistant.imap_client import decode_modified_utf7
from mail_assistant.storage import Store


def billing_block(store: Store) -> bool:
    last = store.conn.execute(
        "SELECT diagnostics_json FROM processing_attempts WHERE stage='llm' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not last:
        return False
    try:
        return json.loads(last[0] or "{}").get("http_status") == 402
    except (ValueError, AttributeError):
        return False


ERROR_LABELS = {
    'OCR_POOR': 'Не распознан: плохой OCR',
    'OCR_UNAVAILABLE': 'Не распознан: OCR недоступен',
    'LLM_FAILED': 'Не обработан: ошибка запроса к AI',
    'LLM_INVALID': 'Не обработан: ответ AI не разобран',
    'LLM_UNCERTAIN': 'Не обработан: исход запроса неизвестен, повтор не выполнялся',
}


def document_library(store: Store) -> list[dict[str, Any]]:
    receipts: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in store.conn.execute("SELECT * FROM receipts WHERE is_current=1 ORDER BY id"):
        receipts[row["document_id"]].append(dict(row))
    sources: dict[int, list[str]] = defaultdict(list)
    for row in store.conn.execute(
        "SELECT s.document_id,s.part_name,mb.name AS mailbox FROM document_sources s "
        "LEFT JOIN messages m ON m.id=s.message_id LEFT JOIN mailboxes mb ON mb.id=m.mailbox_id ORDER BY s.id"
    ):
        label = f"{decode_modified_utf7(row['mailbox'])} / {row['part_name']}" if row['mailbox'] else row['part_name']
        if label not in sources[row['document_id']]:
            sources[row['document_id']].append(label)
    diagnostics = {}
    for row in store.conn.execute("SELECT document_id,diagnostics_json FROM processing_attempts WHERE stage='llm' ORDER BY id"):
        try:
            diagnostics[row['document_id']] = json.loads(row['diagnostics_json'] or '{}')
        except ValueError:
            diagnostics[row['document_id']] = {}
    result = []
    for doc in store.conn.execute("SELECT * FROM documents WHERE is_synthetic=0 ORDER BY id DESC"):
        doc = dict(doc)
        entries = receipts[doc['id']]
        complete = entries and all(r['purpose'] and r['amount_text'] and r['currency'] and r['purchase_date'] for r in entries)
        if entries:
            group = 'with_fields'
            if doc['error_code'] == 'LLM_TRUNCATED' and not any(r['purpose'] for r in entries):
                state = 'Назначение не получено: ответ обрезан'
            else:
                state = 'Поля извлечены · проверьте' if complete else 'Поля извлечены частично'
        elif diagnostics.get(doc['id'], {}).get('http_status') == 402:
            group, state = 'billing', 'AI остановлен: провайдер вернул HTTP 402'
        elif doc['error_code'] == 'NO_TEXT':
            group, state = 'unreadable', 'Нет читаемого текста / возможно, не чек'
        elif doc['error_code'] == 'NOT_RECEIPT':
            group, state = 'other', 'Не найдено признаков чека'
        elif doc['error_code'] == 'LLM_TRUNCATED':
            group, state = 'error', 'Не распознан: ответ обрезан'
        elif doc['error_code']:
            group, state = 'error', ERROR_LABELS.get(doc['error_code'], 'Не обработан')
        elif doc['status'] == 'ai_done':
            group, state = 'other', 'Не платёж: модель не нашла платёж'
        else:
            group, state = 'waiting', 'Текст сохранён · ожидает AI' if doc['extracted_text'] else 'Ожидает извлечения текста'
        result.append({
            **doc, 'group': group, 'state': state, 'receipts': entries,
            'sources': sources[doc['id']],
            'filename': sources[doc['id']][0] if sources[doc['id']] else f"Документ №{doc['id']}",
            'recipient': '; '.join(dict.fromkeys(r['merchant_name'] for r in entries if r['merchant_name'])),
            'purpose': '; '.join(dict.fromkeys(r['purpose'] for r in entries if r['purpose'])),
            'amount': '; '.join(money_label(r) for r in entries) if entries else 'Ещё не распознана',
            'payment_dates': '; '.join(dict.fromkeys(r['purchase_date'] for r in entries if r['purchase_date'])),
        })
    return result


def library_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(item['group'] for item in items)
    return {'total': len(items), **counts,
            'without_purpose': sum(bool(item['receipts']) and not item['purpose'] for item in items)}


def library_table(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        '№': item['id'], 'Дата платежа': item['payment_dates'] or 'Ещё не распознана',
        'Получатель': item['recipient'] or 'Ещё не распознан',
        'Назначение': item['purpose'] or 'Ещё не распознано',
        'Сумма': item['amount'], 'Обработка': item['state'], 'Источник': item['filename'],
    } for item in items]
