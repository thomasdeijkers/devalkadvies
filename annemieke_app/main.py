from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .database import create_db, get_session
from .exporter import budget_document_to_xlsx
from .models import BudgetLine, ExtractedField, IncomingDocument
from .parser import parse_pdf


app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
logo_path = Path(__file__).resolve().parent.parent / "logo.webp"


@app.on_event("startup")
def startup() -> None:
    create_db()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    total = session.scalar(select(func.count(IncomingDocument.id))) or 0
    needs_review = session.scalar(
        select(func.count(IncomingDocument.id)).where(IncomingDocument.status == "needs_review")
    ) or 0
    processed = session.scalar(select(func.count(IncomingDocument.id)).where(IncomingDocument.status == "processed")) or 0
    documents = session.scalars(select(IncomingDocument).order_by(IncomingDocument.created_at.desc()).limit(12)).all()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "total": total,
            "needs_review": needs_review,
            "processed": processed,
            "documents": documents,
        },
    )


@app.get("/logo.webp")
def logo() -> FileResponse:
    return FileResponse(logo_path, media_type="image/webp")


@app.post("/documents", response_class=HTMLResponse)
async def upload_document(file: UploadFile = File(...), session: Session = Depends(get_session)) -> RedirectResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload een PDF-bestand.")

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{uuid4().hex}.pdf"
    target_path = settings.upload_dir / stored_filename
    target_path.write_bytes(await file.read())

    parsed = parse_pdf(target_path)
    document = IncomingDocument(
        original_filename=file.filename,
        stored_filename=stored_filename,
        status="needs_review",
        parsed_text=parsed.text,
        parser_notes=parsed.notes,
    )
    document.fields = [
        ExtractedField(field_name=field.name, field_value=field.value, confidence=field.confidence)
        for field in parsed.fields
    ]
    document.budget_lines = [
        BudgetLine(
            line_number=line.line_number,
            omschrijving_werkzaamheden=line.omschrijving_werkzaamheden,
            hoeveelheid=line.hoeveelheid,
            eenheid=line.eenheid,
            norm_arbeid=line.norm_arbeid,
            uren=line.uren,
            materiaal=line.materiaal,
            materieel=line.materieel,
            onderaannemer=line.onderaannemer,
            totaal_prijs_per_regel=line.totaal_prijs_per_regel,
            eenheidsprijs=line.eenheidsprijs,
            confidence=line.confidence,
            raw_text=line.raw_text,
        )
        for line in parsed.budget_lines
    ]

    session.add(document)
    session.commit()

    return RedirectResponse(f"/documents/{document.id}", status_code=303)


@app.get("/documents/{document_id}", response_class=HTMLResponse)
def document_detail(
    document_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    return templates.TemplateResponse(
        "document_detail.html",
        {"request": request, "app_name": settings.app_name, "document": document},
    )


@app.post("/documents/{document_id}/fields")
def add_field(
    document_id: int,
    field_name: str = Form(...),
    field_value: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    session.add(
        ExtractedField(
            document_id=document.id,
            field_name=field_name.strip().lower().replace(" ", "_"),
            field_value=field_value.strip(),
            confidence=100,
            source="manual",
        )
    )
    document.status = "needs_review"
    session.commit()
    return RedirectResponse(f"/documents/{document.id}", status_code=303)


@app.post("/documents/{document_id}/status")
def update_status(
    document_id: int,
    status: str = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")
    if status not in {"needs_review", "processed", "archived"}:
        raise HTTPException(status_code=400, detail="Onbekende status.")

    document.status = status
    session.commit()
    return RedirectResponse(f"/documents/{document.id}", status_code=303)


@app.get("/documents/{document_id}/export.xlsx")
def export_document(document_id: int, session: Session = Depends(get_session)) -> StreamingResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    stream = budget_document_to_xlsx(document)
    filename = f"begroting-{document.id}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/documents/{document_id}")
def document_json(document_id: int, session: Session = Depends(get_session)) -> dict[str, object]:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    return {
        "id": document.id,
        "filename": document.original_filename,
        "status": document.status,
        "document_type": document.document_type,
        "fields": [
            {
                "name": field.field_name,
                "value": field.field_value,
                "confidence": field.confidence,
                "source": field.source,
            }
            for field in document.fields
        ],
    }
