"""Read-only mail coverage audit. No message bodies, attachments, OCR or LLM."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from mail_assistant.bodystructure import candidate_attachments
from mail_assistant.config import MailSettings
from mail_assistant.imap_client import ImapSession, MailError
from mail_assistant.storage import Store


def audit_coverage(mail: MailSettings, store: Store, start: date, end: date) -> dict[str, Any]:
    with ImapSession(mail) as session:
        folders = session.list_mailboxes()
    rows = []
    for folder in folders:
        if not folder.selectable:
            continue
        try:
            with ImapSession(mail, folder.mailbox_id) as session:
                session.open_mailbox()
                uids = session.search_received(start, end)
                candidates: set[int] = set()
                unknown = 0
                for offset in range(0, len(uids), 50):
                    batch = uids[offset:offset + 50]
                    structures = session.fetch_structures(batch)
                    for uid in batch:
                        found = candidate_attachments(structures[uid], 'all', 0) if uid in structures else None
                        if found is None:
                            unknown += 1
                            candidates.add(uid)
                        elif found:
                            candidates.add(uid)
                imported = {row[0] for row in store.conn.execute(
                    "SELECT DISTINCT m.uid FROM messages m JOIN mailboxes mb ON mb.id=m.mailbox_id "
                    "JOIN accounts a ON a.id=mb.account_id JOIN document_sources s ON s.message_id=m.id "
                    "WHERE a.identifier=? AND mb.name=? AND mb.uidvalidity=?",
                    (mail.username, folder.mailbox_id, session.uidvalidity),
                )}
                rows.append({'folder': folder.display_name, 'mailbox': folder.mailbox_id,
                             'messages': len(uids), 'candidate_messages': len(candidates),
                             'with_local_source': len(candidates & imported),
                             'without_local_source': len(candidates - imported), 'unknown_structure': unknown})
        except MailError as exc:
            rows.append({'folder': folder.display_name, 'mailbox': folder.mailbox_id, 'error': str(exc)})
    return {'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'received_from': start.isoformat(), 'received_to': end.isoformat(), 'folders': rows}
