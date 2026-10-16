"""Local receipt dashboard. Rendering never reads mail or calls a model."""
from __future__ import annotations

import json
from datetime import date, timedelta
from io import BytesIO

import streamlit as st

from mail_assistant import web_services as svc
from mail_assistant.config import AppError, load_env_file
from mail_assistant.document_library import billing_block, document_library, library_counts, library_table
from mail_assistant.periods import three_month_period
from mail_assistant.operation_view import group_operations, operation_summary_rows, operation_table
from mail_assistant.presentation import (
    CATEGORY_LABELS, KIND_LABELS, OPERATION_LABELS, PAYMENT_LABELS,
    issues, money_label, preview_summary, recipient_label, review_label, table_rows,
)
from mail_assistant.receipts import confirm_receipt, duplicate_suspects, edit_receipt, reject_receipt
from mail_assistant.report import report_csv_text
from mail_assistant.storage import CATEGORIES, OPERATION_TYPES

st.set_page_config(page_title="Мои расходы", page_icon="🧾", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
    .block-container {padding-top: 2rem;}
    [data-testid="stMetricValue"] {font-size: clamp(1.5rem, 4.5vw, 2.75rem); font-weight: 650;}
    [data-testid="stMetricValue"] > div {white-space: normal; overflow-wrap: anywhere;}
</style>""", unsafe_allow_html=True)
load_env_file()


def _busy() -> bool:
    return bool(st.session_state.get("busy", False))


def _run(action):
    if _busy():
        return None
    st.session_state["busy"] = True
    try:
        with st.spinner("Выполняется…"):
            result = action()
        return result
    except AppError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f"Ошибка обработки ({type(exc).__name__}). Личные данные скрыты.")
    finally:
        st.session_state["busy"] = False
    return None


def _action_and_refresh(action, message: str):
    def perform():
        action()
        st.session_state["notice"] = message
        st.session_state["refresh_needed"] = True
    _run(perform)
    if st.session_state.pop("refresh_needed", False):
        st.rerun()


def _show_original(mime: str, content: bytes | None, receipt_id: int):
    if content is None:
        st.info("Оригинал не сохранён. Распознанный текст доступен ниже.")
    elif mime == "image/png":
        st.image(content, caption="Исходная квитанция", width="stretch")
    elif mime == "application/pdf":
        import pypdfium2 as pdfium

        try:
            with pdfium.PdfDocument(content) as pdf:
                with pdf[0] as page:
                    scale = min(1.5, 1600 / max(page.get_size()))
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        buffer = BytesIO()
                        image.save(buffer, format="PNG")
                        st.image(buffer.getvalue(), caption=f"PDF · страница 1 из {len(pdf)}", width="stretch")
                    finally:
                        bitmap.close()
        except Exception:
            st.info("Предпросмотр PDF недоступен. Скачайте оригинал.")
        st.download_button("Скачать PDF", content, file_name=f"receipt-{receipt_id}.pdf", key=f"pdf-{receipt_id}")


def _receipt_table(records, duplicates):
    # Keep money and status visible even on a laptop; long legal names must not
    # consume the entire table width and push amounts out of the viewport.
    st.dataframe(
        table_rows(records, duplicates), hide_index=True, width="stretch",
        column_order=["Дата", "Сумма", "Кому / за что", "Статус"],
        column_config={
            "Дата": st.column_config.TextColumn(width="small"),
            "Сумма": st.column_config.TextColumn(width="small"),
            "Кому / за что": st.column_config.TextColumn(width="medium"),
            "Статус": st.column_config.TextColumn(width="medium"),
        },
    )


today = date.today()
with svc.open_store() as store:
    rows = svc.receipt_rows(store)
    suspects = duplicate_suspects(store)
    pending = svc.pending_documents(store)
    selected_model = svc.preferred_model(store)
    library = document_library(store)
    counts = library_counts(library)
    billing_paused = billing_block(store)
    documents = [dict(row) for row in store.conn.execute(
        "SELECT id, status, error_code, mime_type, length(extracted_text) AS chars FROM documents ORDER BY id DESC"
    )]
duplicate_ids = {int(value) for group in suspects for value in group["receipt_ids"].split(",")}
active_rows = [row for row in rows if row["review_status"] != "rejected"]

heading, date_left, date_right = st.columns([2, 1, 1])
heading.title("Мои расходы")
start = date_left.date_input("С", three_month_period(today)[0], key="period_from")
end = date_right.date_input("По", today, key="period_to")
if notice := st.session_state.pop("notice", None):
    st.success(notice)
with st.sidebar:
    st.header("Настройки")
    scope = st.radio("Показывать", ["Все распознанные", "Только подтверждённые"])

if start > end:
    st.error("Начало периода должно быть раньше окончания.")
    st.stop()
period_rows = [row for row in active_rows if row["purchase_date"] and start.isoformat() <= row["purchase_date"] <= end.isoformat()]
if scope == "Только подтверждённые":
    period_rows = [row for row in period_rows if row["review_status"] == "confirmed"]

overview, files, review, add = st.tabs(["Расходы", "Документы", "Проверить записи", "Добавить чеки"])
settings_tab = st.sidebar.expander("Облачная модель")

with overview:
    operations, bills = group_operations(period_rows)
    summary = preview_summary(operation_summary_rows(operations), set())
    if not summary:
        st.caption("За этот период пока нет распознанных платежей с известной суммой и валютой.")
    for currency, bucket in summary.items():
        total, payments = st.columns([2, 1])
        total.metric("Потрачено · " + currency, money_label({"amount_minor": bucket['expense'], "currency": currency}))
        payments.metric("Платежей", bucket['count'])
        st.caption("Предварительно · по распознанным платежам. Данные за период пока неполные.")
        if bucket['refund']:
            refund = money_label({'amount_minor': bucket['refund'], 'currency': currency})
            net = money_label({'amount_minor': bucket['expense'] - bucket['refund'], 'currency': currency})
            st.caption(f"Возвращено {refund} · чистый отток {net}")
        st.markdown("#### Кому отправлены деньги")
        st.dataframe(
            [{"Получатель": name, "Сумма": money_label({"amount_minor": value, "currency": currency})}
             for name, value in sorted(bucket['merchants'].items(), key=lambda pair: -pair[1])],
            hide_index=True, width="stretch", height=min(285, 38 + 35 * len(bucket['merchants'])),
            column_config={"Получатель": st.column_config.TextColumn(width="large"),
                           "Сумма": st.column_config.TextColumn(width="small")},
        )
    st.markdown("#### Все платежи")
    if operations:
        compact = []
        for op in sorted(operations, key=lambda item: (item['date'] or '', item['primary_id']), reverse=True):
            amount = money_label({'amount_minor': op['amount_minor'], 'amount_text': op.get('amount_text'), 'currency': op['currency']})
            compact.append({'Дата': op['date'], 'Кому': recipient_label(op['recipient']),
                            'За что': op['purpose'] if op['purpose_known'] else '—', 'Сумма': amount})
        st.dataframe(compact, hide_index=True, width="stretch", column_order=['Дата', 'Кому', 'Сумма', 'За что'],
                     column_config={'Дата': st.column_config.TextColumn(width="small"),
                                    'Кому': st.column_config.TextColumn(width="medium"),
                                    'Сумма': st.column_config.TextColumn(width="small"),
                                    'За что': st.column_config.TextColumn(width="medium")})
    else:
        st.caption("Платежей за выбранный период пока нет.")
    if bills:
        with st.expander(f"Счета без подтверждения оплаты · {len(bills)}"):
            st.caption("В сумму расходов не включены.")
            st.dataframe(operation_table(bills, money_label), hide_index=True, width="stretch")
    with svc.open_store() as store:
        report = svc.report_with_pending(store, start.isoformat(), end.isoformat())
    st.download_button("Скачать CSV подтверждённых расходов", report_csv_text(report),
                       file_name=f"expenses-{start}-{end}.csv", mime="text/csv")

with review:
    st.subheader("Проверка и исправление")
    st.caption("Выберите документ: слева оригинал, справа распознанные поля. Отсутствие ИНН или времени не мешает учёту.")
    filter_label = st.radio("Записи", ["Все", "Ожидают проверки", "Исключённые"], horizontal=True)
    candidates = rows
    if filter_label == "Ожидают проверки":
        candidates = [row for row in rows if row["review_status"] not in ("confirmed", "rejected")]
    elif filter_label == "Исключённые":
        candidates = [row for row in rows if row["review_status"] == "rejected"]
    if not candidates:
        st.info("Здесь пока нет записей.")
    else:
        _receipt_table(candidates, duplicate_ids)
        by_id = {row["id"]: row for row in candidates}
        choice = st.selectbox("Документ для проверки", list(by_id), format_func=lambda value:
                              f"№{value} · {by_id[value]['purchase_date'] or 'Без даты'} · {by_id[value]['merchant_name'] or 'Без получателя'} · {money_label(by_id[value])}")
        current = by_id[choice]
        st.markdown(f"### {money_label(current)}")
        st.write(f"{KIND_LABELS.get(current['document_kind'], 'Документ')} · {review_label(current, duplicate_ids)}")
        for problem in issues(current):
            st.warning(problem)
        if choice in duplicate_ids:
            st.warning("Возможный дубль. Сравните документы и исключите повтор, прежде чем подтверждать расход.")
        with svc.open_store() as store:
            mime, content = svc.original_path(store, current["document_id"])
            document = dict(store.document(current["document_id"]))
            sources = svc.sources_summary(store, current["document_id"])
        left, right = st.columns([1.1, 1])
        with left:
            _show_original(mime, content, choice)
            with st.expander("Распознанный текст и источник"):
                st.text(document["extracted_text"] or "Текст отсутствует")
                st.caption(sources)
        with right:
            with st.form(f"edit-{choice}"):
                merchant = st.text_input("Получатель / магазин", current["merchant_name"] or "")
                purpose = st.text_area("За что / назначение платежа", current["purpose"] or "", max_chars=300)
                date_text = st.text_input("Дата платежа (YYYY-MM-DD)", current["purchase_date"] or "")
                amount = st.text_input("Сумма платежа", current["amount_text"] or "")
                currency = st.selectbox("Валюта", ["", "BYN", "RUB", "USD", "EUR", "KZT", "GBP", "CNY", "CHF"],
                                        index=["", "BYN", "RUB", "USD", "EUR", "KZT", "GBP", "CNY", "CHF"].index(current["currency"] or ""),
                                        format_func=lambda value: value or "Не определена")
                category = st.selectbox("Категория", CATEGORIES, index=CATEGORIES.index(current["category"]), format_func=CATEGORY_LABELS.get)
                operation = st.selectbox("Операция", OPERATION_TYPES, index=OPERATION_TYPES.index(current["operation_type"]), format_func=OPERATION_LABELS.get)
                payment = st.selectbox("Оплата", list(PAYMENT_LABELS), index=list(PAYMENT_LABELS).index(current["payment_status"]), format_func=PAYMENT_LABELS.get)
                save = st.form_submit_button("Сохранить исправления", disabled=_busy())
            if save:
                pairs = {"merchant": (merchant, current["merchant_name"] or ""), "date": (date_text, current["purchase_date"] or ""),
                         "purpose": (purpose, current["purpose"] or ""),
                         "amount": (amount, current["amount_text"] or ""), "currency": (currency, current["currency"] or ""),
                         "category": (category, current["category"]), "operation": (operation, current["operation_type"]),
                         "payment_status": (payment, current["payment_status"])}
                changes = {key: new for key, (new, old) in pairs.items() if new != old}
                if changes:
                    def save_action():
                        with svc.open_store() as store:
                            edit_receipt(store, choice, changes)
                    _action_and_refresh(save_action, "Исправления сохранены")
            a, b = st.columns(2)
            if a.button("Подтвердить", disabled=_busy() or current["review_status"] == "confirmed" or bool(issues(current))):
                def confirm_action():
                    with svc.open_store() as store:
                        confirm_receipt(store, choice)
                _action_and_refresh(confirm_action, "Расход подтверждён")
            if b.button("Отклонить", disabled=_busy() or current["review_status"] == "rejected"):
                def reject_action():
                    with svc.open_store() as store:
                        reject_receipt(store, choice)
                _action_and_refresh(reject_action, "Запись исключена из расходов")
            with st.expander("Цитаты и подробности распознавания"):
                for key, value in json.loads(current["evidence_json"]).items():
                    if value:
                        st.text(f"{key}: {value}")
                for warning in json.loads(current["warnings_json"]):
                    st.caption(warning)

with add:
    st.subheader("Добавить PNG или PDF")
    st.caption("Сначала текст извлекается локально. Отправка в облачный AI — отдельной кнопкой ниже.")
    if st.button("Проверить охват всех папок без загрузки файлов", disabled=_busy()):
        _action_and_refresh(lambda: svc.audit_mail_coverage(start - timedelta(days=14), end), "Папки проверены. Результат — во вкладке «Все документы».")
    uploads = st.file_uploader("Чеки и квитанции", type=["png", "pdf"], accept_multiple_files=True)
    if st.button("Загрузить файлы", disabled=_busy() or not uploads):
        _action_and_refresh(lambda: svc.import_uploads([(item.name, item.getvalue()) for item in uploads]), "Файлы сохранены. Запустите распознавание ниже.")
    with st.expander("Загрузить из Mail.ru"):
        if st.button("Загрузить список папок", disabled=_busy()):
            folders = _run(svc.list_folders)
            if folders is not None:
                st.session_state["folders"] = [(info.mailbox_id, info.display_name) for info in folders]
        folders = st.session_state.get("folders", [])
        mailbox = st.selectbox("Папка", [item[0] for item in folders], format_func=lambda value: next(name for key, name in folders if key == value)) if folders else None
        c1, c2 = st.columns(2)
        received_from = c1.date_input("Письма с", today - timedelta(days=90))
        received_to = c2.date_input("Письма по", today)
        max_messages = st.number_input("Просмотреть писем", 1, 200, 30)
        kind_label = st.selectbox("Вложения", list(svc.KIND_CHOICES))
        max_documents = st.number_input("Загрузить новых документов", 1, 10, 5)
        if st.button("Загрузить чеки из почты", disabled=_busy() or not mailbox):
            def import_action():
                lines, stats = svc.import_mailbox(mailbox, received_from, received_to, int(max_messages), svc.KIND_CHOICES[kind_label], int(max_documents))
                st.session_state["import_log"] = lines
            _action_and_refresh(import_action, "Загрузка завершена")
        for line in st.session_state.get("import_log", []):
            st.caption(line)
    st.divider()
    st.subheader("Распознать сохранённые документы")
    billing_retry = False
    if billing_paused:
        billing_retry = st.checkbox("Я решил вопрос с оплатой/лимитом и разрешаю повторную попытку AI")
    cloud = svc.cloud_status(billing_retry=billing_retry)
    if not cloud.allowed:
        st.caption("Распознавание временно недоступно. Файлы можно добавлять и просматривать.")
        with st.expander("Подробности обработки"):
            st.info(cloud.reason)
    st.caption(f"Ожидают распознавания: {len(pending)} документов.")
    ids = st.multiselect("Документы для распознавания", [row["id"] for row in pending],
                         default=[row["id"] for row in pending[:3]], format_func=lambda value: f"Документ №{value}")
    if st.button("Распознать новые", disabled=_busy() or not cloud.allowed or not ids):
        def recognize_action():
            with svc.open_store() as store:
                results = svc.recognize_documents(store, st.session_state.get("model", selected_model), ids, billing_retry=billing_retry)
            st.session_state["recognition_log"] = [f"Документ №{result.document_id}: " + (f"ошибка {result.error_code}" if result.error_code else "готов") for result in results]
        _action_and_refresh(recognize_action, "Обработка завершена. Результаты — во вкладке проверки.")
    if st.session_state.get("recognition_log"):
        with st.expander("Подробности последнего запуска"):
            for line in st.session_state['recognition_log']:
                st.write(line)

with files:
    st.subheader("Документы")
    st.caption(f"Всего {counts['total']} · с извлечёнными данными {counts.get('with_fields', 0)}. Здесь также доступны ещё не распознанные файлы.")
    coverage = svc.saved_mail_coverage()
    if coverage:
        missing = sum(row.get('without_local_source', 0) for row in coverage['folders'])
        with st.expander("Охват папок почты — где ещё остались вложения"):
            st.caption(f"Без сохранённых источников: {missing} писем-кандидатов. Среди вложений могут быть иконки и копии, не только чеки.")
            st.caption(f"Проверка {coverage['checked_at']}. По дате получения: {coverage['received_from']} — {coverage['received_to']}. "
                       "Это число писем с PNG/PDF, а не число уникальных платежей. Наличие одного источника не доказывает импорт всех вложений письма.")
            st.dataframe([{'Папка': row['folder'], 'Писем в периоде': row.get('messages'),
                           'С PNG/PDF или неизвестной структурой': row.get('candidate_messages'),
                           'Есть сохранённый источник': row.get('with_local_source'),
                           'Ни одного источника не сохранено': row.get('without_local_source'),
                           'Ошибка': row.get('error', '')} for row in coverage['folders']], hide_index=True, width="stretch")
    groups = {"Все": None, "Есть данные": ('with_fields',), "Ожидают обработки": ('waiting', 'billing'),
              "Нужна проверка": ('unreadable', 'error'), "Без платежа": ('other',)}
    state_labels = {'with_fields': 'Есть данные', 'waiting': 'Ожидает обработки', 'billing': 'Ожидает обработки',
                    'unreadable': 'Нужна проверка', 'error': 'Нужна проверка', 'other': 'Без платежа'}
    group = st.selectbox("Обработка документов", list(groups))
    query = st.text_input("Поиск по получателю, назначению или источнику").strip().casefold()
    visible = [item for item in library if (groups[group] is None or item['group'] in groups[group])
               and (not query or query in ' '.join((item['filename'], item['recipient'], item['purpose'])).casefold())]
    st.caption(f"Показано {len(visible)} из {len(library)} загруженных документов.")
    if visible:
        display_documents = library_table(visible)
        for display_row, item in zip(display_documents, visible):
            display_row['Обработка'] = state_labels[item['group']]
        st.dataframe(display_documents, hide_index=True, width="stretch",
                     column_order=["№", "Дата платежа", "Сумма", "Получатель", "Назначение", "Обработка", "Источник"])
        by_document = {item['id']: item for item in visible}
        default_document = next((index for index, item in enumerate(visible) if item['purpose']), 0)
        document_id = st.selectbox("Открыть документ", list(by_document), format_func=lambda value:
                                   f"№{value} · {by_document[value]['recipient'] or by_document[value]['filename']}", index=default_document)
        selected = by_document[document_id]
        left, right = st.columns(2)
        with left:
            with svc.open_store() as store:
                mime, content = svc.original_path(store, document_id)
            _show_original(mime, content, document_id + 1000000)
        with right:
            st.markdown(f"**Сумма:** {selected['amount']}")
            st.write("Получатель: " + (selected['recipient'] or 'Ещё не распознан'))
            st.write("Назначение: " + (selected['purpose'] or '—'))
            st.write("Дата платежа: " + (selected['payment_dates'] or 'Ещё не распознана'))
            st.caption("Источники")
            for source in selected['sources']:
                st.text(source)
            if selected['receipts']:
                st.caption("Исправление и подтверждение — во вкладке «Проверить записи».")
        with st.expander("Распознанный текст документа"):
            st.text(selected['extracted_text'] or 'Читаемый текст пока не получен')
    else:
        st.info("Документов по этому фильтру нет.")
    with st.expander("Технические подробности"):
        if billing_paused:
            st.caption("Облачный провайдер вернул HTTP 402. Обработка приостановлена до решения вопроса с оплатой/лимитом.")
        st.dataframe(documents, hide_index=True, width="stretch")

with settings_tab:
    st.subheader("Облачная модель")
    st.selectbox("Модель", svc.COMPATIBLE_MODELS, index=svc.COMPATIBLE_MODELS.index(selected_model), key="model")
    st.caption("Выбранная модель используется только при нажатии «Распознать новые». Локальные LLM не используются.")
    st.caption("Неудачные и неопределённые запросы не повторяются сами. Оригиналы и результаты хранятся локально.")
