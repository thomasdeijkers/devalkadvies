from decimal import Decimal, InvalidOperation
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
from .models import BudgetLine, ExtractedField, IncomingDocument, Project, Relation
from .parser import parse_pdf


app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
logo_path = Path(__file__).resolve().parent.parent / "logo.webp"


@app.on_event("startup")
def startup() -> None:
    create_db()


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    project: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    document_query = select(IncomingDocument)
    if project:
        search = f"%{project.strip()}%"
        document_query = document_query.outerjoin(IncomingDocument.project).where(
            (IncomingDocument.project_name.ilike(search)) | (Project.name.ilike(search)) | (Project.project_number.ilike(search))
        )
    if status:
        document_query = document_query.where(IncomingDocument.status == status)

    total = session.scalar(select(func.count(IncomingDocument.id))) or 0
    needs_review = session.scalar(
        select(func.count(IncomingDocument.id)).where(IncomingDocument.status == "needs_review")
    ) or 0
    processed = session.scalar(select(func.count(IncomingDocument.id)).where(IncomingDocument.status == "processed")) or 0
    documents = session.scalars(document_query.order_by(IncomingDocument.created_at.desc()).limit(30)).all()
    projects = session.scalars(
        select(Project).order_by(Project.created_at.desc(), Project.name).limit(18)
    ).all()
    relations = session.scalars(
        select(Relation).order_by(Relation.created_at.desc(), Relation.name).limit(18)
    ).all()
    project_options = session.scalars(
        select(Project).order_by(Project.name)
    ).all()
    relation_options = session.scalars(
        select(Relation).order_by(Relation.name)
    ).all()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "app_name": settings.app_name,
            "total": total,
            "needs_review": needs_review,
            "processed": processed,
            "documents": documents,
            "projects": projects,
            "relations": relations,
            "project_options": project_options,
            "relation_options": relation_options,
            "selected_project": project or "",
            "selected_status": status or "",
        },
    )


@app.get("/logo.webp")
def logo() -> FileResponse:
    return FileResponse(logo_path, media_type="image/webp")


@app.post("/documents", response_class=HTMLResponse)
async def upload_document(
    project_name: str = Form(""),
    project_id: str = Form(""),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload een PDF-bestand.")

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{uuid4().hex}.pdf"
    target_path = settings.upload_dir / stored_filename
    target_path.write_bytes(await file.read())

    parsed = parse_pdf(target_path)
    selected_project = session.get(Project, _int_or_none(project_id)) if _int_or_none(project_id) else None
    document = IncomingDocument(
        original_filename=file.filename,
        stored_filename=stored_filename,
        project_id=selected_project.id if selected_project else None,
        project_name=project_name.strip() or None,
        status="needs_review",
        source=parsed.parse_method,
        parsed_text=parsed.text,
        parser_notes=_parser_note(parsed),
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


@app.post("/relations")
def create_relation(
    relation_type: str = Form("opdrachtgever"),
    name: str = Form(...),
    contact_name: str = Form(""),
    address: str = Form(""),
    postal_code: str = Form(""),
    city: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    website: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    session.add(
        Relation(
            relation_type=relation_type.strip() or "opdrachtgever",
            name=name.strip(),
            contact_name=contact_name.strip() or None,
            address=address.strip() or None,
            postal_code=postal_code.strip() or None,
            city=city.strip() or None,
            phone=phone.strip() or None,
            email=email.strip() or None,
            website=website.strip() or None,
            notes=notes.strip() or None,
        )
    )
    session.commit()
    return RedirectResponse("/#relations", status_code=303)


@app.post("/relations/{relation_id}")
def update_relation(
    relation_id: int,
    relation_type: str = Form("opdrachtgever"),
    name: str = Form(...),
    contact_name: str = Form(""),
    address: str = Form(""),
    postal_code: str = Form(""),
    city: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    website: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    relation = session.get(Relation, relation_id)
    if relation is None:
        raise HTTPException(status_code=404, detail="Relatie niet gevonden.")
    relation.relation_type = relation_type.strip() or "opdrachtgever"
    relation.name = name.strip()
    relation.contact_name = contact_name.strip() or None
    relation.address = address.strip() or None
    relation.postal_code = postal_code.strip() or None
    relation.city = city.strip() or None
    relation.phone = phone.strip() or None
    relation.email = email.strip() or None
    relation.website = website.strip() or None
    relation.notes = notes.strip() or None
    session.commit()
    return RedirectResponse(f"/#relation-{relation.id}", status_code=303)


@app.post("/projects")
def create_project(
    project_number: str = Form(""),
    name: str = Form(...),
    description: str = Form(""),
    location: str = Form(""),
    status: str = Form("actief"),
    client_relation_id: str = Form(""),
    architect_relation_id: str = Form(""),
    constructor_relation_id: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    session.add(
        Project(
            project_number=project_number.strip() or None,
            name=name.strip(),
            description=description.strip() or None,
            location=location.strip() or None,
            status=status.strip() or "actief",
            client_relation_id=_int_or_none(client_relation_id),
            architect_relation_id=_int_or_none(architect_relation_id),
            constructor_relation_id=_int_or_none(constructor_relation_id),
            notes=notes.strip() or None,
        )
    )
    session.commit()
    return RedirectResponse("/#projects", status_code=303)


@app.post("/projects/{project_id}")
def update_project(
    project_id: int,
    project_number: str = Form(""),
    name: str = Form(...),
    description: str = Form(""),
    location: str = Form(""),
    status: str = Form("actief"),
    client_relation_id: str = Form(""),
    architect_relation_id: str = Form(""),
    constructor_relation_id: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project niet gevonden.")
    project.project_number = project_number.strip() or None
    project.name = name.strip()
    project.description = description.strip() or None
    project.location = location.strip() or None
    project.status = status.strip() or "actief"
    project.client_relation_id = _int_or_none(client_relation_id)
    project.architect_relation_id = _int_or_none(architect_relation_id)
    project.constructor_relation_id = _int_or_none(constructor_relation_id)
    project.notes = notes.strip() or None
    session.commit()
    return RedirectResponse(f"/#project-{project.id}", status_code=303)


@app.post("/documents/{document_id}/reparse-openai")
def reparse_document_with_openai(
    document_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    file_path = settings.upload_dir / document.stored_filename
    parsed = parse_pdf(file_path, force_openai=True)
    document.source = parsed.parse_method
    document.parsed_text = parsed.text
    document.parser_notes = _parser_note(parsed)
    document.status = "needs_review"
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
        request=request,
        name="document_detail.html",
        context={
            "request": request,
            "app_name": settings.app_name,
            "document": document,
            "project_options": session.scalars(select(Project).order_by(Project.name)).all(),
        },
    )


@app.post("/documents/{document_id}/meta")
def update_document_meta(
    document_id: int,
    project_id: str = Form(""),
    project_name: str = Form(""),
    document_type: str = Form(""),
    source: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    selected_project = session.get(Project, _int_or_none(project_id)) if _int_or_none(project_id) else None
    document.project_id = selected_project.id if selected_project else None
    document.project_name = project_name.strip() or (selected_project.name if selected_project else None)
    document.document_type = document_type.strip() or None
    document.source = source.strip() or None
    session.commit()
    return RedirectResponse(f"/documents/{document.id}", status_code=303)


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
    return RedirectResponse(request_url_for_document(document.id, archived=status == "archived"), status_code=303)


@app.post("/documents/{document_id}/delete")
def delete_document(
    document_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    file_path = settings.upload_dir / document.stored_filename
    session.delete(document)
    session.commit()
    if file_path.exists():
        file_path.unlink()
    return RedirectResponse("/", status_code=303)


@app.post("/documents/{document_id}/lines/{line_id}")
def update_budget_line(
    document_id: int,
    line_id: int,
    omschrijving_werkzaamheden: str = Form(""),
    hoeveelheid: str = Form(""),
    eenheid: str = Form(""),
    norm_arbeid: str = Form(""),
    uren: str = Form(""),
    materiaal: str = Form(""),
    materieel: str = Form(""),
    onderaannemer: str = Form(""),
    eenheidsprijs: str = Form(""),
    totaal_prijs_per_regel: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    line = session.get(BudgetLine, line_id)
    if line is None or line.document_id != document_id:
        raise HTTPException(status_code=404, detail="Begrotingsregel niet gevonden.")

    line.omschrijving_werkzaamheden = omschrijving_werkzaamheden.strip()
    line.hoeveelheid = _decimal_or_none(hoeveelheid)
    line.eenheid = eenheid.strip() or None
    line.norm_arbeid = _decimal_or_none(norm_arbeid)
    line.uren = _decimal_or_none(uren)
    line.materiaal = _decimal_or_none(materiaal)
    line.materieel = _decimal_or_none(materieel)
    line.onderaannemer = _decimal_or_none(onderaannemer)
    line.eenheidsprijs = _decimal_or_none(eenheidsprijs)
    line.totaal_prijs_per_regel = _decimal_or_none(totaal_prijs_per_regel)
    line.confidence = 100
    session.commit()
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@app.post("/documents/{document_id}/lines")
def add_budget_line(
    document_id: int,
    omschrijving_werkzaamheden: str = Form(""),
    hoeveelheid: str = Form(""),
    eenheid: str = Form(""),
    norm_arbeid: str = Form(""),
    uren: str = Form(""),
    materiaal: str = Form(""),
    materieel: str = Form(""),
    onderaannemer: str = Form(""),
    eenheidsprijs: str = Form(""),
    totaal_prijs_per_regel: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    next_line_number = len(document.budget_lines) + 1
    session.add(
        BudgetLine(
            document_id=document.id,
            line_number=next_line_number,
            omschrijving_werkzaamheden=omschrijving_werkzaamheden.strip(),
            hoeveelheid=_decimal_or_none(hoeveelheid),
            eenheid=eenheid.strip() or None,
            norm_arbeid=_decimal_or_none(norm_arbeid),
            uren=_decimal_or_none(uren),
            materiaal=_decimal_or_none(materiaal),
            materieel=_decimal_or_none(materieel),
            onderaannemer=_decimal_or_none(onderaannemer),
            eenheidsprijs=_decimal_or_none(eenheidsprijs),
            totaal_prijs_per_regel=_decimal_or_none(totaal_prijs_per_regel),
            confidence=100,
            raw_text="handmatig toegevoegd",
        )
    )
    session.commit()
    return RedirectResponse(f"/documents/{document.id}", status_code=303)


@app.post("/documents/{document_id}/lines/{line_id}/delete")
def delete_budget_line(
    document_id: int,
    line_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    line = session.get(BudgetLine, line_id)
    if line is None or line.document_id != document_id:
        raise HTTPException(status_code=404, detail="Begrotingsregel niet gevonden.")

    session.delete(line)
    session.commit()
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


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
        "project_name": document.project_name,
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


def _decimal_or_none(value: str) -> Decimal | None:
    cleaned = value.strip().replace(" ", "")
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _int_or_none(value: str | int | None) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def request_url_for_document(document_id: int, archived: bool = False) -> str:
    return "/" if archived else f"/documents/{document_id}"


def _parser_note(parsed) -> str | None:
    parts = [f"Parse methode: {parsed.parse_method}", f"zekerheid: {parsed.confidence}%"]
    if parsed.openai_usage:
        parts.append(f"OpenAI tokens: {parsed.openai_usage.get('total_tokens', 0)}")
    if parsed.notes:
        parts.append(parsed.notes)
    return " | ".join(parts)
