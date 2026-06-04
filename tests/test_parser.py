from annemieke_app.parser import _extract_fields, extract_budget_lines


def test_extracts_key_value_fields() -> None:
    fields = _extract_fields("Naam: Annemieke\nDossiernummer: 12345\n")

    values = {field.name: field.value for field in fields}

    assert values["naam"] == "Annemieke"
    assert values["dossiernummer"] == "12345"


def test_extracts_date_and_amount_fallbacks() -> None:
    fields = _extract_fields("Factuur ontvangen op 04-06-2026 voor EUR 125,50")

    values = {field.name: field.value for field in fields}

    assert values["datum"] == "04-06-2026"
    assert values["bedrag"] == "125,50"


def test_extracts_budget_line_from_table_text() -> None:
    lines = extract_budget_lines("18mm MDF 583,31 m2 78,93 46.041,00")

    assert len(lines) == 1
    assert lines[0].omschrijving_werkzaamheden == "18mm MDF"
    assert str(lines[0].hoeveelheid) == "583.31"
    assert lines[0].eenheid == "m2"
    assert str(lines[0].eenheidsprijs) == "78.93"
    assert str(lines[0].totaal_prijs_per_regel) == "46041.00"


def test_extracts_wrapped_budget_line() -> None:
    lines = extract_budget_lines("18mm MDF plafondplaat\n583,31 m2 78,93 46.041,00")

    assert len(lines) == 1
    assert lines[0].omschrijving_werkzaamheden == "18mm MDF plafondplaat"
    assert str(lines[0].hoeveelheid) == "583.31"
    assert str(lines[0].totaal_prijs_per_regel) == "46041.00"
