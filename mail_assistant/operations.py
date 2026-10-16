"""Финансовые операции: несколько документов одного платежа дают одну операцию.

Автообъединение только по сильным идентификаторам (номер операции ЕРИП, RRN) и только если
сумма, валюта, дата и получатель не противоречат друг другу. Каждая связь хранит основание и
обратима: отмена ставит undone_at, исходные документы не удаляются.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from mail_assistant.config import AppError
from mail_assistant.storage import Store, utc_now

STRONG_IDENTIFIERS = frozenset({"erip_operation", "rrn"})
_IDENTIFIER_PATTERNS = {
    "erip_operation": re.compile(r"(?:номер|№|N)\s*операции\s*в\s*ЕРИП\s*:?\s*(\d{6,})", re.I),
    "rrn": re.compile(r"\bRRN\s*:?\s*(\d{6,})", re.I),
    "auth_code": re.compile(r"код авторизации\s*:?\s*([A-Za-z0-9]{4,})", re.I),
    "check_number": re.compile(r"чек\s*(?:№|N[оo]?)\s*(\d{6,})", re.I),
    "unp": re.compile(r"\bУНП\s*:?\s*(\d{9})\b", re.I),
}


class OperationError(AppError):
    """Ошибка связывания операций; текст безопасен для вывода."""


def extract_identifiers(text: str) -> list[dict[str, str]]:
    """Идентификаторы из распознанного текста. Не выдумываются: только явные подписи."""
    found: list[dict[str, str]] = []
    for kind, pattern in _IDENTIFIER_PATTERNS.items():
        for match in pattern.finditer(text):
            found.append({"type": kind, "value": match.group(1)})
    return found


def identifiers_of(row: dict[str, Any]) -> list[dict[str, str]]:
    raw = row.get("identifiers_json")
    return json.loads(raw) if raw else []


def _norm_recipient(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"[^0-9a-zа-яё]+", " ", value.lower()).strip() or None


@dataclass
class LinkPlan:
    identifier: dict[str, str]
    receipt_ids: list[int]
    linked: bool
    reason: str
    contradictions: list[str] = field(default_factory=list)


def _contradictions(rows: list[dict[str, Any]]) -> list[str]:
    problems = []
    currencies = {row["currency"] for row in rows if row["currency"]}
    if len(currencies) > 1:
        problems.append("разные валюты")
    amounts = {row["debit_minor"] or row["amount_minor"] for row in rows if (row["debit_minor"] or row["amount_minor"]) is not None}
    if len(amounts) > 1:
        problems.append("разные суммы")
    dates = {row["purchase_date"] for row in rows if row["purchase_date"]}
    if len(dates) > 1:
        problems.append("разные даты")
    recipients = {_norm_recipient(row["merchant_name"]) for row in rows if row["merchant_name"]}
    if len(recipients) > 1:
        problems.append("разные получатели")
    kinds = {row["operation_type"] for row in rows if row["operation_type"] in ("expense", "refund")}
    if len(kinds) > 1:
        problems.append("расход и возврат с одним идентификатором")
    return problems


def plan_links(store: Store) -> list[LinkPlan]:
    """Ищет группы документов с одинаковым сильным идентификатором. Ничего не записывает."""
    rows = [
        dict(row)
        for row in store.conn.execute(
            "SELECT r.* FROM receipts r WHERE r.is_current = 1 AND r.review_status != 'rejected' "
            "AND r.identifiers_json IS NOT NULL"
        ).fetchall()
    ]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for identifier in identifiers_of(row):
            if identifier["type"] in STRONG_IDENTIFIERS:
                groups[(identifier["type"], identifier["value"])].append(row)
    plans = []
    for (kind, value), members in sorted(groups.items()):
        ids = sorted(row["id"] for row in members)
        if len(members) < 2:
            continue
        problems = _contradictions(members)
        plans.append(
            LinkPlan(
                identifier={"type": kind, "value": value},
                receipt_ids=ids,
                linked=not problems,
                reason=(
                    f"совпал идентификатор {kind}; сумма, валюта, дата и получатель не противоречат"
                    if not problems
                    else "не объединено: " + ", ".join(problems)
                ),
                contradictions=problems,
            )
        )
    return plans


def apply_plan(store: Store, plan: LinkPlan) -> int:
    """Записывает одну операцию для группы без противоречий. Возвращает id операции."""
    if not plan.linked:
        raise OperationError("Группа содержит противоречия и не объединяется автоматически")
    members = [dict(row) for row in store.conn.execute(
        f"SELECT * FROM receipts WHERE id IN ({','.join('?' * len(plan.receipt_ids))})", plan.receipt_ids
    ).fetchall()]
    primary = min(members, key=lambda row: row["id"])
    basis = f"{plan.identifier['type']}={plan.identifier['value']}; {plan.reason}"
    now = utc_now()
    with store.conn:
        existing = store.conn.execute(
            "SELECT DISTINCT operation_id FROM receipts WHERE id IN ({}) AND operation_id IS NOT NULL".format(
                ",".join("?" * len(plan.receipt_ids))
            ),
            plan.receipt_ids,
        ).fetchall()
        if existing:
            operation_id = existing[0][0]
        else:
            cursor = store.conn.execute(
                "INSERT INTO operations (kind, amount_minor, currency, operation_date, recipient, purpose, description, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    primary["operation_type"], primary["debit_minor"] or primary["amount_minor"], primary["currency"],
                    primary["purchase_date"], primary["merchant_name"], primary["purpose"], primary["description"],
                    now, now,
                ),
            )
            operation_id = int(cursor.lastrowid)
        for row in members:
            if row["operation_id"] == operation_id:
                continue
            store.conn.execute("UPDATE receipts SET operation_id = ?, is_primary = ?, updated_at = ? WHERE id = ?",
                               (operation_id, int(row["id"] == primary["id"]), now, row["id"]))
            store.conn.execute(
                "INSERT INTO operation_links (receipt_id, operation_id, basis, created_at) VALUES (?, ?, ?, ?)",
                (row["id"], operation_id, basis, now),
            )
    return operation_id


def unlink_receipt(store: Store, receipt_id: int) -> None:
    """Отменяет связь: документ снова самостоятельная запись. История связей сохраняется."""
    row = store.receipt(receipt_id)
    if row is None:
        raise OperationError("Чек с таким номером не найден")
    if row["operation_id"] is None:
        raise OperationError("Документ не связан с операцией")
    now = utc_now()
    with store.conn:
        store.conn.execute(
            "UPDATE operation_links SET undone_at = ? WHERE receipt_id = ? AND undone_at IS NULL", (now, receipt_id)
        )
        store.conn.execute(
            "UPDATE receipts SET operation_id = NULL, is_primary = 0, updated_at = ? WHERE id = ?", (now, receipt_id)
        )


COPY_SIMILARITY = 0.95


def plan_text_copies(store: Store) -> list[LinkPlan]:
    """Копии одного документа: почти весь текст совпадает, а получатель, дата, сумма и валюта равны.

    Совпадения только по сумме и дате не достаточно. Порог сходства строк 0.95.
    """
    import difflib

    rows = [
        dict(row)
        for row in store.conn.execute(
            "SELECT r.*, d.sha256, d.extracted_text FROM receipts r JOIN documents d ON d.id = r.document_id "
            "WHERE r.is_current = 1 AND r.review_status != 'rejected' AND d.extracted_text IS NOT NULL"
        ).fetchall()
    ]
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["merchant_name"], row["purchase_date"], row["amount_minor"], row["currency"], row["document_kind"])].append(row)
    plans: list[LinkPlan] = []
    for key, members in groups.items():
        if len(members) < 2 or key[0] is None or key[1] is None or key[2] is None:
            continue
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                if left["document_id"] == right["document_id"]:
                    continue
                ratio = difflib.SequenceMatcher(
                    None, (left["extracted_text"] or "").splitlines(), (right["extracted_text"] or "").splitlines()
                ).ratio()
                if ratio < COPY_SIMILARITY:
                    continue
                ids = sorted([left["id"], right["id"]])
                plans.append(
                    LinkPlan(
                        identifier={"type": "copy", "value": f"{left['sha256'][:12]}~{right['sha256'][:12]}"},
                        receipt_ids=ids,
                        linked=True,
                        reason=f"копия документа: текст совпадает на {ratio:.2f}; получатель, дата, сумма и валюта равны",
                    )
                )
    return plans
