from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .models import IncomingDocument


HEADERS = [
    "Omschrijving / werkzaamheden",
    "Hvh",
    "Ehd",
    "Norm / arbeid",
    "Uren",
    "Materiaal",
    "Materieel",
    "O.A.",
    "Totaal prijs per regel",
    "Eenheidsprijs",
]


def budget_document_to_xlsx(document: IncomingDocument) -> BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Begroting"

    sheet.append(HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill(fill_type="solid", fgColor="EAF0F3")
        cell.alignment = Alignment(horizontal="center")

    for line in document.budget_lines:
        sheet.append(
            [
                line.omschrijving_werkzaamheden,
                line.hoeveelheid,
                line.eenheid,
                line.norm_arbeid,
                line.uren,
                line.materiaal,
                line.materieel,
                line.onderaannemer,
                line.totaal_prijs_per_regel,
                line.eenheidsprijs,
            ]
        )

    widths = [42, 12, 8, 12, 10, 13, 13, 13, 18, 14]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width

    for row in sheet.iter_rows(min_row=2):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
        for cell in row[1:]:
            cell.alignment = Alignment(horizontal="right")

    stream = BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream
