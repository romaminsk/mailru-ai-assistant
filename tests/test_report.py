from __future__ import annotations

import csv
from decimal import Decimal

import pytest

import mail_assistant.receipts as receipts_module
from receipt_fixtures import FakeLLM, FakeOcr, ai_options, completion, receipt_settings
from synthetic_docs import make_pdf
from mail_assistant.money import amount_to_minor, minor_to_text, parse_amount
from mail_assistant.receipts import SourceRef, confirm_receipt, edit_receipt, process_document
from mail_assistant.report import ReportError, build_report, parse_period, render_report, safe_cell, write_csv
from mail_assistant.storage import Store


def _receipt_payload(*, date_text, merchant, amount, currency, operation, category="food") -> tuple[bytes, str]:
    ddmmyyyy = f"{date_text[8:10]}.{date_text[5:7]}.{date_text[0:4]}"
    keyword = "RETURN" if operation == "refund" else "SALE"
    lines = [merchant, f"{ddmmyyyy} 12:00", f"{keyword} {amount} {currency}", f"ITOGO {amount} {currency}", f"Doc {date_text} {amount} {merchant}"]
    item = {
        "merchant_name": merchant,
        "merchant_tax_id": None,
        "purchase_date": date_text,
        "purchase_time": None,
        "currency": currency,
        "total_amount": amount,
        "operation_type": operation,
        "category": category,
        "receipt_number": None,
        "fiscal_id": None,
        "evidence": {
            "merchant": merchant,
            "date": ddmmyyyy,
            "amount": f"ITOGO {amount} {currency}",
            "operation": keyword,
        },
        "warnings": [],
    }
    import json

    return make_pdf([{"lines": lines}]), json.dumps({"receipts": [item]}, ensure_ascii=False)


def _add_confirmed(store, settings, **fields) -> int:
    data, content = _receipt_payload(**fields)
    fake = FakeLLM(lambda request: completion(content))
    ai = ai_options(fake, budget=5)
    result = process_document(store, data, source=SourceRef(part_name="x"), settings=settings, ocr=FakeOcr(), ai=ai)
    receipt_id = result.receipt_ids[0]
    confirm_receipt(store, receipt_id)
    return receipt_id


@pytest.fixture
def settings(tmp_path):
    return receipt_settings(tmp_path)


@pytest.fixture
def store(settings):
    with Store(settings.db_path) as handle:
        yield handle


def test_decimal_arithmetic_is_exact():
    assert minor_to_text(amount_to_minor(parse_amount("0.10")) + amount_to_minor(parse_amount("0.20"))) == "0.30"
    assert parse_amount("1 234,50") == Decimal("1234.50")
    assert parse_amount("1,234.50") == Decimal("1234.50")
    assert parse_amount("1234.5") == Decimal("1234.50")
    assert minor_to_text(-5) == "-0.05"
    assert amount_to_minor(Decimal("900.00")) == 90000


@pytest.mark.parametrize("bad", ["1e3", "-5", "12.345", "abc", "", "NaN", "1.2.3"])
def test_invalid_amounts_are_rejected(bad):
    from mail_assistant.money import MoneyError

    with pytest.raises(MoneyError):
        parse_amount(bad)


def test_period_boundaries_are_inclusive_by_purchase_date(store, settings):
    _add_confirmed(store, settings, date_text="2026-09-30", merchant="A", amount="10.00", currency="RUB", operation="expense")
    _add_confirmed(store, settings, date_text="2026-10-01", merchant="B", amount="20.00", currency="RUB", operation="expense")
    _add_confirmed(store, settings, date_text="2026-10-31", merchant="C", amount="30.00", currency="RUB", operation="expense")
    _add_confirmed(store, settings, date_text="2026-11-01", merchant="D", amount="40.00", currency="RUB", operation="expense")

    report = build_report(store, "2026-10-01", "2026-10-31")

    assert report["currencies"]["RUB"]["expense"] == "50.00"
    assert report["confirmed_total"] == 2


def test_refund_is_subtracted_and_net_is_shown(store, settings):
    _add_confirmed(store, settings, date_text="2026-10-05", merchant="Shop", amount="900.00", currency="RUB", operation="expense")
    _add_confirmed(store, settings, date_text="2026-10-06", merchant="Shop", amount="250.00", currency="RUB", operation="refund")

    data = build_report(store, "2026-10-01", "2026-10-31")["currencies"]["RUB"]

    assert data["expense"] == "900.00"
    assert data["refund"] == "250.00"
    assert data["net"] == "650.00"


def test_currencies_are_reported_separately(store, settings):
    _add_confirmed(store, settings, date_text="2026-10-05", merchant="A", amount="10.00", currency="RUB", operation="expense")
    _add_confirmed(store, settings, date_text="2026-10-05", merchant="B", amount="7.25", currency="USD", operation="expense")

    report = build_report(store, "2026-10-01", "2026-10-31")

    assert report["currencies"]["RUB"]["expense"] == "10.00"
    assert report["currencies"]["USD"]["expense"] == "7.25"
    assert set(report["currencies"]) == {"RUB", "USD"}


def test_unconfirmed_records_are_counted_only_as_pending(store, settings):
    data, content = _receipt_payload(date_text="2026-10-05", merchant="Pending", amount="99.00", currency="RUB", operation="expense")
    result = process_document(
        store, data, source=SourceRef(part_name="p"), settings=settings, ocr=FakeOcr(),
        ai=ai_options(FakeLLM(lambda r: completion(content))),
    )
    assert result.receipt_ids

    report = build_report(store, "2026-10-01", "2026-10-31")

    assert report["currencies"] == {}
    assert report["pending_in_period"] == 1


def test_undated_pending_is_counted_separately(store, settings):
    data, content = _receipt_payload(date_text="2026-10-05", merchant="NoDate", amount="5.00", currency="RUB", operation="expense")
    import json

    item = json.loads(content)["receipts"][0]
    item["purchase_date"] = None
    item["evidence"]["date"] = None
    content = json.dumps({"receipts": [item]})
    process_document(
        store, data, source=SourceRef(part_name="nd"), settings=settings, ocr=FakeOcr(),
        ai=ai_options(FakeLLM(lambda r: completion(content))),
    )

    report = build_report(store, "2026-10-01", "2026-10-31")

    assert report["pending_without_date"] == 1
    assert report["pending_in_period"] == 0


def test_report_groups_by_merchant_and_category(store, settings):
    _add_confirmed(store, settings, date_text="2026-10-05", merchant="Market", amount="100.00", currency="RUB", operation="expense", category="food")
    _add_confirmed(store, settings, date_text="2026-10-07", merchant="Market", amount="50.00", currency="RUB", operation="expense", category="food")
    _add_confirmed(store, settings, date_text="2026-10-08", merchant="Cafe", amount="20.00", currency="RUB", operation="expense", category="food")

    rendered = "\n".join(render_report(build_report(store, "2026-10-01", "2026-10-31")))

    assert "Market: расход 150.00" in rendered
    assert "Cafe: расход 20.00" in rendered
    assert "Валюта RUB" in rendered


def test_manual_correction_changes_report_total(store, settings):
    receipt_id = _add_confirmed(store, settings, date_text="2026-10-05", merchant="Shop", amount="900.00", currency="RUB", operation="expense")
    edit_receipt(store, receipt_id, {"amount": "950.00"})

    assert build_report(store, "2026-10-01", "2026-10-31")["currencies"]["RUB"]["expense"] == "950.00"


def test_report_reads_only_database(store, settings, monkeypatch):
    _add_confirmed(store, settings, date_text="2026-10-05", merchant="Shop", amount="1.00", currency="RUB", operation="expense")

    def forbidden(*args, **kwargs):
        raise AssertionError("отчёт не должен обращаться к IMAP или LLM")

    monkeypatch.setattr(receipts_module, "LLMClient", forbidden)
    monkeypatch.setattr("mail_assistant.imap_client.ImapSession.__enter__", forbidden)
    report = build_report(store, "2026-10-01", "2026-10-31")
    assert report["currencies"]["RUB"]["expense"] == "1.00"


def test_period_validation():
    assert parse_period("2026-10-01", "2026-10-31") == ("2026-10-01", "2026-10-31")
    with pytest.raises(ReportError):
        parse_period("2026-10-31", "2026-10-01")
    with pytest.raises(ReportError):
        parse_period("01.10.2026", "2026-10-31")


def test_csv_escapes_formula_injection(store, settings, tmp_path):
    receipt_id = _add_confirmed(store, settings, date_text="2026-10-05", merchant="Shop", amount="9.00", currency="RUB", operation="expense")
    store.update_receipt(receipt_id, {"merchant_name": '=HYPERLINK("http://x","y")'})
    report = build_report(store, "2026-10-01", "2026-10-31")
    path = tmp_path / "export.csv"

    count = write_csv(report, path)

    assert count == 1
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    merchant_cell = rows[1][2]
    assert merchant_cell.startswith("'=")
    assert not any(cell.startswith("=") for row in rows for cell in row)


def test_safe_cell_prefixes_dangerous_starts():
    assert safe_cell("+1") == "'+1"
    assert safe_cell("@SUM(A1)") == "'@SUM(A1)"
    assert safe_cell("-2") == "'-2"
    assert safe_cell("\tcmd") == "'\tcmd"
    assert safe_cell("Обычный магазин") == "Обычный магазин"


def test_csv_refuses_overwrite_without_force_and_is_private(store, settings, tmp_path):
    import os
    import stat

    _add_confirmed(store, settings, date_text="2026-10-05", merchant="Shop", amount="9.00", currency="RUB", operation="expense")
    report = build_report(store, "2026-10-01", "2026-10-31")
    path = tmp_path / "out.csv"
    write_csv(report, path)

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with pytest.raises(ReportError):
        write_csv(report, path)
    write_csv(report, path, force=True)


def test_results_survive_restart_and_report_without_llm(settings, monkeypatch):
    with Store(settings.db_path) as first:
        _add_confirmed(first, settings, date_text="2026-10-05", merchant="Shop", amount="42.00", currency="RUB", operation="expense")
    with Store(settings.db_path) as second:
        assert build_report(second, "2026-10-01", "2026-10-31")["currencies"]["RUB"]["expense"] == "42.00"


def test_acceptance_two_receipts_and_refund_survive_restart(settings, monkeypatch):
    with Store(settings.db_path) as first:
        _add_confirmed(first, settings, date_text="2026-10-03", merchant="Market", amount="900.00", currency="RUB", operation="expense")
        _add_confirmed(first, settings, date_text="2026-10-12", merchant="Cafe", amount="350.50", currency="RUB", operation="expense")
        _add_confirmed(first, settings, date_text="2026-10-15", merchant="Market", amount="120.25", currency="RUB", operation="refund")

    def forbidden(*args, **kwargs):
        raise AssertionError("после перезапуска не должно быть обращений к почте или LLM")

    monkeypatch.setattr(receipts_module, "LLMClient", forbidden)
    monkeypatch.setattr("mail_assistant.imap_client.ImapSession.__enter__", forbidden)
    with Store(settings.db_path) as second:
        report = build_report(second, "2026-10-01", "2026-10-31")

    data = report["currencies"]["RUB"]
    assert data["expense"] == "1250.50"
    assert data["refund"] == "120.25"
    assert data["net"] == "1130.25"
    assert data["confirmed_receipts"] == 3
    assert report["confirmed_total"] == 3
