"""Local receipt dashboard. Rendering never reads mail or calls a model."""
from __future__ import annotations

import json
from datetime import date, timedelta
from io import BytesIO

import streamlit as st

from mail_assistant import web_services as svc
from mail_assistant.config import AppError, load_env_file
from mail_assistant.presentation import (
    CATEGORY_LABELS, KIND_LABELS, OPERATION_LABELS, PAYMENT_LABELS,
    document_label, issues, money_label, preview_summary, review_label, table_rows,
)
from mail_assistant.receipts import confirm_receipt, duplicate_suspects, edit_receipt, reject_receipt
from mail_assistant.report import report_csv_text
from mail_assistant.storage import CATEGORIES, OPERATION_TYPES

st.set_page_config(page_title="Мои расходы", page_icon="🧾", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
    .block-container {padding-top: 2rem;}
    [data-testid="stMetricValue"] {font-size: clamp(1.25rem, 4vw, 2rem);}
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
    documents = [dict(row) for row in store.conn.execute(
        "SELECT id, status, error_code, mime_type, length(extracted_text) AS chars FROM documents ORDER BY id DESC"
    )]
duplicate_ids = {int(value) for group in suspects for value in group["receipt_ids"].split(",")}
active_rows = [row for row in rows if row["review_status"] != "rejected"]

st.title("Мои расходы")
st.caption("PNG и PDF → расходы и платежи. Данные сохранены на этом компьютере.")
if notice := st.session_state.pop("notice", None):
    st.success(notice)

with st.sidebar:
    st.header("Период расходов")
    start = st.date_input("С", today.replace(day=1), key="period_from")
    end = st.date_input("По", today, key="period_to")
    st.caption("По дате покупки или платежа, не по дате письма.")
    scope = st.radio("Показывать", ["Все распознанные", "Только подтверждённые"])
    st.divider()
    st.caption("Безопасность: письма только читаются. Оплата, отправка писем и перемещение не выполняются.")

if start > end:
    st.error("Начало периода должно быть раньше окончания.")
    st.stop()
period_rows = [row for row in active_rows if row["purchase_date"] and start.isoformat() <= row["purchase_date"] <= end.isoformat()]
if scope == "Только подтверждённые":
    period_rows = [row for row in period_rows if row["review_status"] == "confirmed"]

overview, review, add, files, settings_tab = st.tabs(["Расходы", "Проверить записи", "Добавить чеки", "Документы", "Настройки AI"])

with overview:
    st.subheader("Куда ушли деньги")
    st.caption(f"{start:%d.%m.%Y} — {end:%d.%m.%Y} · изменить период: кнопка » в левом верхнем углу.")
    st.markdown("Предварительно: включая непроверенные платежи. Счета без оплаты и возможные дубли исключены.")
    summary = preview_summary(period_rows, duplicate_ids)
    if not summary:
        st.info("За этот период нет платежей с полной суммой, валютой и датой. Откройте «Проверить записи» или добавьте чеки.")
    for currency, bucket in summary.items():
        st.markdown(f"### {currency}")
        metrics = st.columns(2)
        metrics[0].metric("Расходы", money_label({"amount_minor": bucket['expense'], "currency": currency}))
        metrics[1].metric("Возвраты", money_label({"amount_minor": bucket['refund'], "currency": currency}))
        net = money_label({"amount_minor": bucket['expense'] - bucket['refund'], "currency": currency})
        st.write(f"Чистый отток: **{net}** · платежей: **{bucket['count']}**")
        if bucket["pending"]:
            st.caption(f"Из них ещё не подтверждены вами: {bucket['pending']}. Погашение кредита — отток денег, не покупка товара.")
        left, right = st.columns(2)
        with left:
            st.markdown("**Получатели платежей**")
            st.dataframe([{"Кому": name, "Сумма": money_label({"amount_minor": value, "currency": currency})}
                          for name, value in sorted(bucket["merchants"].items(), key=lambda pair: -pair[1])], hide_index=True, width="stretch")
        with right:
            st.markdown("**По категориям**")
            st.dataframe([{"Категория": name, "Сумма": money_label({"amount_minor": value, "currency": currency})}
                          for name, value in sorted(bucket["categories"].items(), key=lambda pair: -pair[1])], hide_index=True, width="stretch")
    st.markdown("### Все документы за период")
    if period_rows:
        _receipt_table(period_rows, duplicate_ids)
    invoices = sum(row["document_kind"] == "invoice" and row["payment_status"] != "paid" for row in period_rows)
    if invoices:
        st.info(f"Счетов / начислений без подтверждения оплаты: {invoices}. Они показаны в списке, но не считаются потраченными деньгами.")
    undated = sum(not row["purchase_date"] for row in active_rows)
    if undated:
        st.warning(f"Без даты: {undated}. Они не попали в период; исправьте их во вкладке проверки.")
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
    cloud = svc.cloud_status()
    if not cloud.allowed:
        st.info(cloud.reason)
    st.caption(f"Готовы к AI: {len(pending)}. Модель: {st.session_state.get('model', selected_model)}. Повторная отрисовка страницы не отправляет запросы.")
    ids = st.multiselect("Документы для распознавания", [row["id"] for row in pending],
                         default=[row["id"] for row in pending[:3]], format_func=lambda value: f"Документ №{value}")
    if st.button("Распознать новые", disabled=_busy() or not cloud.allowed or not ids):
        def recognize_action():
            with svc.open_store() as store:
                results = svc.recognize_documents(store, st.session_state.get("model", selected_model), ids)
            st.session_state["recognition_log"] = [f"Документ №{result.document_id}: " + (f"ошибка {result.error_code}" if result.error_code else "готов") for result in results]
        _action_and_refresh(recognize_action, "Обработка завершена. Результаты — во вкладке проверки.")
    for line in st.session_state.get("recognition_log", []):
        st.write(line)

with files:
    st.subheader("Состояние документов")
    st.caption("Не путайте обработку файла и подтверждение расхода: обработанный файл ещё нужно проверить.")
    st.dataframe([{"Документ": row["id"], "Формат": row["mime_type"].split("/")[-1].upper(),
                   "Состояние": document_label(row), "Символов текста": row["chars"] or 0} for row in documents],
                 hide_index=True, width="stretch")
    with st.expander("Технические подробности"):
        st.dataframe(documents, hide_index=True, width="stretch")

with settings_tab:
    st.subheader("Облачная модель")
    st.selectbox("Модель", svc.COMPATIBLE_MODELS, index=svc.COMPATIBLE_MODELS.index(selected_model), key="model")
    st.caption("Выбранная модель используется только при нажатии «Распознать новые». Локальные LLM не используются.")
    st.caption("Неудачные и неопределённые запросы не повторяются сами. Оригиналы и результаты хранятся локально.")
