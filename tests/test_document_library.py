from __future__ import annotations

import json
from datetime import date

import httpx

from receipt_fixtures import FakeLLM, FakeOcr, RECEIPT_TEXT_LINES, ai_options, receipt_settings
from synthetic_docs import make_pdf
from mail_assistant.document_library import billing_block, document_library, library_counts, library_table
from mail_assistant.operation_view import group_operations, operation_summary_rows
from mail_assistant.presentation import preview_summary
from mail_assistant.receipt_extract import verify_receipts
from mail_assistant.receipt_schemas import ReceiptBatch, ReceiptItem
from mail_assistant.receipts import SourceRef, edit_receipt, process_document
from mail_assistant.storage import Store


def add_document(store, number, code=None):
    doc = store.create_document(f'file-{number}', 'image/png', 3000)
    store.set_document_text(doc, 'Пример чека\nИТОГО 100.00 BYN', True, 1, 'text_extracted')
    store.add_source(doc, None, f'payment-{number}.png')
    if code:
        store.set_document_status(doc, 'text_extracted', code)
    return doc


def add_receipt(store, doc):
    text = 'Магазин\n15.08.2026\nИТОГО 100.00 BYN\nОплата'
    store.set_document_text(doc, text, True, 1, 'text_extracted')
    batch = ReceiptBatch(receipts=[ReceiptItem(
        merchant_name='Магазин', purchase_date='2026-08-15', total_amount='100.00', currency='BYN',
        operation_type='expense', evidence={'merchant': 'Магазин', 'date': '15.08.2026',
                                          'amount': 'ИТОГО 100.00 BYN', 'operation': 'Оплата'})])
    store.save_result(doc, None, verify_receipts(batch, text, True))
    return store.current_receipts(doc)[0]['id']


def test_library_keeps_unrecognised_undated_and_failed_documents(tmp_path):
    with Store(tmp_path / 'db.sqlite3') as store:
        doc = add_document(store, 1)
        add_receipt(store, doc)
        blocked = add_document(store, 2, 'LLM_FAILED')
        store.record_attempt(blocked, stage='llm', status='failed', error_code='LLM_FAILED', started_at='x',
                             extractor_version='test', diagnostics={'http_status': 402})
        add_document(store, 3)
        no_text = add_document(store, 4, 'NO_TEXT')
        store.set_document_status(no_text, 'failed', 'NO_TEXT')
        synthetic = add_document(store, 5)
        store.mark_synthetic(synthetic)
        items = document_library(store)
        counts = library_counts(items)
        assert counts['total'] == 4
        assert counts['with_fields'] == counts['billing'] == counts['waiting'] == counts['unreadable'] == 1
        assert len(library_table(items)) == 4
        assert all('synthetic' not in item['filename'] for item in items)
        assert billing_block(store) is True
        assert next(item for item in items if item['id'] == blocked)['receipts'] == []


def test_existing_august_payment_is_visible_and_summarised_without_amount_text(tmp_path):
    with Store(tmp_path / 'db.sqlite3') as store:
        doc = add_document(store, 1)
        add_receipt(store, doc)
        rows = [dict(r) for r in store.conn.execute('SELECT * FROM receipts WHERE is_current=1')]
        operations, _ = group_operations(rows)
        assert operations[0]['date'] == '2026-08-15'
        summary = preview_summary(operation_summary_rows(operations), set())
        assert summary['BYN']['expense'] == 10000
        assert '100,00 BYN' == document_library(store)[0]['amount']


def test_editing_purpose_is_supported_and_audited(tmp_path):
    with Store(tmp_path / 'db.sqlite3') as store:
        doc = add_document(store, 1)
        rid = add_receipt(store, doc)
        edit_receipt(store, rid, {'purpose': 'Покупка продуктов'})
        assert store.receipt(rid)['purpose'] == 'Покупка продуктов'
        assert store.corrections_for(rid)[-1]['field'] == 'purpose'


def test_first_http402_stops_remaining_batch_requests(tmp_path):
    settings = receipt_settings(tmp_path)
    fake = FakeLLM(lambda request: httpx.Response(402, text='balance unavailable'))
    ai = ai_options(fake, budget=10)
    with Store(settings.db_path) as store:
        results = [process_document(store, make_pdf([{'lines': RECEIPT_TEXT_LINES + [str(index)]}]),
                                   source=SourceRef(part_name=f'{index}.pdf'), settings=settings,
                                   ocr=FakeOcr(), ai=ai) for index in range(3)]
        assert len(fake.calls) == 1
        assert ai.budget.used == 1
        assert all(result.error_code == 'LLM_PAYMENT_REQUIRED' for result in results)
        assert billing_block(store)
        latest = store.conn.execute("SELECT diagnostics_json FROM processing_attempts WHERE stage='llm' ORDER BY id DESC LIMIT 1").fetchone()
        assert json.loads(latest[0])['http_status'] == 402


def test_coverage_audits_structures_without_downloading_messages(tmp_path, monkeypatch):
    import mail_assistant.mail_coverage as module
    from mail_assistant.imap_client import MailboxInfo

    class Session:
        uidvalidity = 10

        def __init__(self, settings, mailbox='INBOX'):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def list_mailboxes(self):
            return [MailboxInfo('INBOX', 'Входящие', frozenset())]

        def open_mailbox(self):
            return self.list_mailboxes()[0], 3

        def search_received(self, start, end):
            return [1, 2, 3]

        def fetch_structures(self, uids):
            return {1: b'pdf', 2: b'pdf', 3: b'none'}

        def fetch_message(self, uid):
            raise AssertionError('Audit must not download message bodies')

    monkeypatch.setattr(module, 'ImapSession', Session)
    monkeypatch.setattr(module, 'candidate_attachments', lambda raw, *args: [object()] if raw == b'pdf' else [])
    with Store(tmp_path / 'db.sqlite3') as store:
        doc = add_document(store, 1)
        mb = store.ensure_mailbox('example@test.invalid', 'INBOX', 10)
        msg = store.ensure_message(mb, 1, None)
        store.add_source(doc, msg, 'r.pdf')
        class Mail:
            username = 'example@test.invalid'
        report = module.audit_coverage(Mail(), store, date(2026, 7, 2), date(2026, 10, 16))
        assert report['folders'][0]['candidate_messages'] == 2
        assert report['folders'][0]['with_local_source'] == 1
        assert report['folders'][0]['without_local_source'] == 1
