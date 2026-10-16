

def test_erip_intermediary_is_not_shown_as_supplier():
    from mail_assistant.presentation import recipient_label

    assert recipient_label('ОАО "НКФО "ЕРИП", УНП 807000268') == "Через ЕРИП · поставщик в чеке не указан"
    assert recipient_label('КУП "Расчётно-информационный центр Мингорисполкома"').startswith("КУП")
