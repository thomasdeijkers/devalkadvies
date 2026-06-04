import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session, aliased

from .config import settings
from .database import create_db, engine, get_session
from .exporter import budget_document_to_xlsx, selected_budget_lines_to_xlsx
from .index_provider import sync_price_index_series
from .models import (
    BudgetLine,
    ExtractedField,
    IncomingDocument,
    OpenAIUsageEvent,
    PriceIndexSeries,
    Project,
    Relation,
    ScheduledJob,
)
from .parser import parse_pdf


app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
logo_path = Path(__file__).resolve().parent.parent / "logo.webp"
APP_STARTED_AT = time.time()


def euro(value: Decimal | int | float | str | None) -> str:
    if value is None or value == "":
        return ""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€ {formatted}"


def amount(value: Decimal | int | float | str | None) -> str:
    if value is None or value == "":
        return ""
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    if number == number.to_integral_value():
        return str(number.quantize(Decimal("1")))
    formatted = f"{number.quantize(Decimal('0.01')):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return formatted.rstrip("0").rstrip(",")


def calculated_unit_price(line: BudgetLine) -> Decimal | None:
    if line.totaal_prijs_per_regel is not None and line.hoeveelheid not in {None, 0}:
        try:
            return line.totaal_prijs_per_regel / line.hoeveelheid
        except (InvalidOperation, ZeroDivisionError):
            return line.eenheidsprijs
    return line.eenheidsprijs


templates.env.filters["euro"] = euro
templates.env.filters["amount"] = amount
templates.env.filters["unit_price"] = calculated_unit_price


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
    return _render_workspace(request, session, "overview", project, status)


@app.get("/documents", response_class=HTMLResponse)
def documents_page(
    request: Request,
    project: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return _render_workspace(request, session, "documents", project, status)


@app.get("/consult", response_class=HTMLResponse)
def consult_page(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    priced: str | None = None,
    min_score: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return _render_workspace(
        request,
        session,
        "consult",
        status=status,
        query=q,
        date_from=date_from,
        date_to=date_to,
        priced=priced,
        min_score=min_score,
    )


@app.get("/relations", response_class=HTMLResponse)
def relations_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _render_workspace(request, session, "relations")


@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _render_workspace(request, session, "projects")


@app.get("/server", response_class=HTMLResponse)
def server_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _render_workspace(request, session, "server")


@app.get("/indices", response_class=HTMLResponse)
def indices_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _render_workspace(request, session, "indices")


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _render_workspace(request, session, "jobs")


def _render_workspace(
    request: Request,
    session: Session,
    active_page: str,
    project: str | None = None,
    status: str | None = None,
    query: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    priced: str | None = None,
    min_score: str | None = None,
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
    index_series = session.scalars(select(PriceIndexSeries).order_by(PriceIndexSeries.name)).all()
    scheduled_jobs = session.scalars(select(ScheduledJob).order_by(ScheduledJob.created_at.desc())).all()
    status_context = _status_context(session)
    consult_lines = _consult_lines(session, query, status, date_from, date_to, priced, min_score) if active_page == "consult" else []

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
            "index_series": index_series,
            "scheduled_jobs": scheduled_jobs,
            "selected_project": project or "",
            "selected_query": query or "",
            "selected_status": status or "",
            "selected_date_from": date_from or "",
            "selected_date_to": date_to or "",
            "selected_priced": priced or "",
            "selected_min_score": min_score or "",
            "consult_lines": consult_lines,
            "active_page": active_page,
            **status_context,
        },
    )


@app.get("/logo.webp")
def logo() -> FileResponse:
    return FileResponse(logo_path, media_type="image/webp")


@app.get("/documents/{document_id}/original.pdf")
def original_pdf(document_id: int, session: Session = Depends(get_session)) -> FileResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")
    file_path = settings.upload_dir / document.stored_filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="PDF niet gevonden.")
    return FileResponse(file_path, media_type="application/pdf", filename=document.original_filename)


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
    _record_openai_usage(session, "budget_upload", document.id, parsed.openai_usage)

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
    return RedirectResponse("/relations", status_code=303)


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
    return RedirectResponse(f"/relations#relation-{relation.id}", status_code=303)


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
    return RedirectResponse("/projects", status_code=303)


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
    return RedirectResponse(f"/projects#project-{project.id}", status_code=303)


@app.post("/indices")
def create_index_series(
    name: str = Form(...),
    description: str = Form(""),
    source: str = Form(""),
    provider: str = Form("cbs"),
    api_url: str = Form(""),
    period_field: str = Form(""),
    value_field: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    series = PriceIndexSeries(
        name=name.strip(),
        description=description.strip() or None,
        source=source.strip() or None,
        provider=provider.strip() or "manual",
        api_url=api_url.strip() or None,
        period_field=period_field.strip() or None,
        value_field=value_field.strip() or None,
    )
    session.add(series)
    session.commit()
    return RedirectResponse("/indices", status_code=303)


@app.post("/indices/{series_id}/sync")
def sync_index_series(series_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    series = session.get(PriceIndexSeries, series_id)
    if series is None:
        raise HTTPException(status_code=404, detail="Indexreeks niet gevonden.")
    sync_price_index_series(session, series)
    return RedirectResponse("/indices", status_code=303)


@app.post("/jobs")
def create_scheduled_job(
    name: str = Form(...),
    job_type: str = Form("index_sync"),
    cron_expression: str = Form("0 5 1 * *"),
    target_id: str = Form(""),
    enabled: str = Form("1"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    session.add(
        ScheduledJob(
            name=name.strip(),
            job_type=job_type.strip() or "index_sync",
            cron_expression=cron_expression.strip() or "0 5 1 * *",
            target_id=_int_or_none(target_id),
            enabled=1 if enabled else 0,
        )
    )
    session.commit()
    return RedirectResponse("/jobs", status_code=303)


@app.post("/jobs/{job_id}/run")
def run_scheduled_job(job_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    job = session.get(ScheduledJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Cronjob niet gevonden.")
    try:
        count = _run_job(session, job)
        job.last_status = "ok"
        job.last_message = f"{count} waarden bijgewerkt"
    except Exception as exc:
        job.last_status = "fout"
        job.last_message = str(exc)[:500]
    job.last_run_at = datetime.now(timezone.utc)
    session.commit()
    return RedirectResponse("/jobs", status_code=303)


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
    _record_openai_usage(session, "budget_reparse", document.id, parsed.openai_usage)
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
            "relation_options": session.scalars(select(Relation).order_by(Relation.name)).all(),
            **_status_context(session),
        },
    )


@app.post("/documents/{document_id}/meta")
def update_document_meta(
    document_id: int,
    project_id: str = Form(""),
    relation_id: str = Form(""),
    project_name: str = Form(""),
    document_type: str = Form(""),
    source: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    selected_project = session.get(Project, _int_or_none(project_id)) if _int_or_none(project_id) else None
    selected_relation = session.get(Relation, _int_or_none(relation_id)) if _int_or_none(relation_id) else None
    document.project_id = selected_project.id if selected_project else None
    document.project_name = project_name.strip() or (selected_project.name if selected_project else None)
    document.document_type = document_type.strip() or None
    document.source = source.strip() or None
    if selected_project and selected_relation:
        selected_project.client_relation_id = selected_relation.id
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
    _normalize_line_prices(line)
    line.confidence = 100
    session.commit()
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@app.post("/documents/{document_id}/budget-lines/bulk")
async def update_budget_lines_bulk(
    document_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    form = await request.form()
    line_ids = form.getlist("line_id")
    delete_line_ids = {str(value) for value in form.getlist("delete_line_id")}
    for index, raw_line_id in enumerate(line_ids):
        try:
            line_id = int(str(raw_line_id))
        except ValueError:
            continue

        line = session.get(BudgetLine, line_id)
        if line is None or line.document_id != document_id:
            continue
        if str(line_id) in delete_line_ids:
            session.delete(line)
            continue

        line.omschrijving_werkzaamheden = _form_value(form, "omschrijving_werkzaamheden", index).strip()
        line.hoeveelheid = _decimal_or_none(_form_value(form, "hoeveelheid", index))
        line.eenheid = _form_value(form, "eenheid", index).strip() or None
        line.norm_arbeid = _decimal_or_none(_form_value(form, "norm_arbeid", index))
        line.uren = _decimal_or_none(_form_value(form, "uren", index))
        line.materiaal = _decimal_or_none(_form_value(form, "materiaal", index))
        line.materieel = _decimal_or_none(_form_value(form, "materieel", index))
        line.onderaannemer = _decimal_or_none(_form_value(form, "onderaannemer", index))
        line.eenheidsprijs = _decimal_or_none(_form_value(form, "eenheidsprijs", index))
        line.totaal_prijs_per_regel = _decimal_or_none(_form_value(form, "totaal_prijs_per_regel", index))
        _normalize_line_prices(line)
        line.confidence = 100

    action = str(form.get("action") or "save")
    if action == "validate":
        document.status = "processed"
    elif action != "delete_selected":
        document.status = "needs_review"
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
        line := BudgetLine(
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
    _normalize_line_prices(line)
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


@app.get("/consult/export.xlsx")
def export_consult_selection(
    q: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    priced: str | None = None,
    min_score: str | None = None,
    session: Session = Depends(get_session),
) -> StreamingResponse:
    lines = _consult_lines(session, q, status, date_from, date_to, priced, min_score, limit=5000)
    stream = selected_budget_lines_to_xlsx(lines)
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="raadplegen-selectie.xlsx"'},
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


def _consult_lines(
    session: Session,
    query: str | None,
    status: str | None,
    date_from: str | None,
    date_to: str | None,
    priced: str | None = None,
    min_score: str | None = None,
    limit: int = 300,
) -> list[BudgetLine]:
    client_relation = aliased(Relation)
    architect_relation = aliased(Relation)
    constructor_relation = aliased(Relation)
    statement = (
        select(BudgetLine)
        .join(BudgetLine.document)
        .outerjoin(IncomingDocument.project)
        .outerjoin(client_relation, Project.client_relation_id == client_relation.id)
        .outerjoin(architect_relation, Project.architect_relation_id == architect_relation.id)
        .outerjoin(constructor_relation, Project.constructor_relation_id == constructor_relation.id)
    )
    cleaned_query = (query or "").strip()
    if cleaned_query:
        search = f"%{cleaned_query}%"
        statement = statement.where(
            or_(
                IncomingDocument.original_filename.ilike(search),
                IncomingDocument.project_name.ilike(search),
                IncomingDocument.document_type.ilike(search),
                IncomingDocument.source.ilike(search),
                Project.name.ilike(search),
                Project.project_number.ilike(search),
                Project.location.ilike(search),
                client_relation.name.ilike(search),
                architect_relation.name.ilike(search),
                constructor_relation.name.ilike(search),
                BudgetLine.omschrijving_werkzaamheden.ilike(search),
                BudgetLine.eenheid.ilike(search),
            )
        )
    if status:
        statement = statement.where(IncomingDocument.status == status)
    if priced:
        statement = statement.where(
            or_(
                BudgetLine.eenheidsprijs.is_not(None),
                and_(
                    BudgetLine.totaal_prijs_per_regel.is_not(None),
                    BudgetLine.hoeveelheid.is_not(None),
                    BudgetLine.hoeveelheid != 0,
                ),
            )
        )
    minimum_score = _int_or_none(min_score)
    if minimum_score is not None:
        statement = statement.where(BudgetLine.confidence >= minimum_score)
    parsed_from = _date_or_none(date_from)
    if parsed_from:
        statement = statement.where(IncomingDocument.created_at >= parsed_from)
    parsed_to = _date_or_none(date_to)
    if parsed_to:
        statement = statement.where(IncomingDocument.created_at < parsed_to + timedelta(days=1))

    return session.scalars(
        statement.order_by(IncomingDocument.created_at.desc(), BudgetLine.line_number).limit(limit)
    ).unique().all()


def _run_job(session: Session, job: ScheduledJob) -> int:
    if job.job_type == "index_sync":
        if job.target_id:
            series = session.get(PriceIndexSeries, job.target_id)
            if series is None:
                raise ValueError("Indexreeks niet gevonden.")
            return sync_price_index_series(session, series)
        total = 0
        for series in session.scalars(select(PriceIndexSeries).where(PriceIndexSeries.api_url.is_not(None))).all():
            total += sync_price_index_series(session, series)
        return total
    raise ValueError("Onbekend jobtype.")


def _date_or_none(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _form_value(form, key: str, index: int) -> str:
    values = form.getlist(key)
    if index >= len(values):
        return ""
    return str(values[index] or "")


def _normalize_line_prices(line: BudgetLine) -> None:
    if line.totaal_prijs_per_regel is not None and line.hoeveelheid not in {None, 0}:
        try:
            line.eenheidsprijs = line.totaal_prijs_per_regel / line.hoeveelheid
        except (InvalidOperation, ZeroDivisionError):
            pass


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


def _record_openai_usage(session: Session, source: str, source_id: int, usage: dict | None) -> None:
    if not usage:
        return
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    event = OpenAIUsageEvent(
        source=source,
        source_id=source_id,
        model=model,
        input_tokens=int(usage.get("input_tokens") or 0),
        cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        estimated_cost_usd=_estimate_openai_cost(model, usage),
    )
    session.add(event)
    session.commit()


def _status_context(session: Session) -> dict[str, object]:
    db_status = _database_status()
    openai_usage = _openai_usage_summary(session)
    server_info = _server_info(db_status)
    return {
        "db_status": db_status,
        "openai_usage": openai_usage,
        "server_info": server_info,
    }


def _database_status() -> dict[str, object]:
    started = time.perf_counter()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            database_name = connection.execute(text("SELECT current_database()")).scalar_one_or_none()
            try:
                db_size = connection.execute(text("SELECT pg_size_pretty(pg_database_size(current_database()))")).scalar_one_or_none()
            except Exception:
                db_size = "-"
        return {
            "connected": True,
            "label": "connected",
            "database": database_name or "database",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "size": db_size or "-",
        }
    except Exception as exc:
        return {
            "connected": False,
            "label": "offline",
            "database": "database",
            "latency_ms": None,
            "size": "-",
            "error": str(exc)[:120],
        }


def _openai_usage_summary(session: Session) -> dict[str, object]:
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total_requests = session.scalar(select(func.count(OpenAIUsageEvent.id))) or 0
    month_requests = session.scalar(
        select(func.count(OpenAIUsageEvent.id)).where(OpenAIUsageEvent.created_at >= month_start)
    ) or 0
    month_tokens = session.scalar(
        select(func.coalesce(func.sum(OpenAIUsageEvent.total_tokens), 0)).where(OpenAIUsageEvent.created_at >= month_start)
    ) or 0
    month_cost = session.scalar(
        select(func.coalesce(func.sum(OpenAIUsageEvent.estimated_cost_usd), 0)).where(OpenAIUsageEvent.created_at >= month_start)
    ) or Decimal("0")
    return {
        "enabled": bool(os.getenv("OPENAI_API_KEY")),
        "month_requests": int(month_requests),
        "total_requests": int(total_requests),
        "month_tokens": int(month_tokens),
        "month_cost_usd": Decimal(str(month_cost or 0)),
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
    }


def _server_info(db_status: dict[str, object]) -> dict[str, object]:
    return {
        "uptime": _format_duration(int(time.time() - APP_STARTED_AT)),
        "load": _load_percent(),
        "memory": _memory_info(),
        "database": db_status,
        "ocr_enabled": os.getenv("OCR_ENABLED", "true").strip().lower() not in {"0", "false", "nee", "no"},
        "openai_enabled": bool(os.getenv("OPENAI_API_KEY")),
    }


def _load_percent() -> dict[str, object]:
    try:
        load_1 = os.getloadavg()[0]
        cores = os.cpu_count() or 1
        return {"percent": min(999, round((load_1 / cores) * 100, 1)), "label": f"load {load_1:.2f} - {cores} cores"}
    except Exception:
        return {"percent": 0, "label": "niet beschikbaar"}


def _memory_info() -> dict[str, object]:
    try:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            meminfo[key] = int(value.strip().split()[0])
        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        used = max(total - available, 0)
        percent = round((used / total) * 100, 1) if total else 0
        return {"percent": percent, "label": f"{used / 1024 / 1024:.1f} GB / {total / 1024 / 1024:.1f} GB"}
    except Exception:
        return {"percent": 0, "label": "niet beschikbaar"}


def _format_duration(seconds: int) -> str:
    delta = timedelta(seconds=max(seconds, 0))
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}u"
    if hours:
        return f"{hours}u {minutes}m"
    return f"{minutes}m"


def _estimate_openai_cost(model: str, usage: dict) -> Decimal:
    pricing = {
        "gpt-4.1-mini": {"input": Decimal("0.25"), "cached_input": Decimal("0.025"), "output": Decimal("2.00")},
        "gpt-4.1": {"input": Decimal("2.00"), "cached_input": Decimal("0.50"), "output": Decimal("8.00")},
        "gpt-5-mini": {"input": Decimal("0.25"), "cached_input": Decimal("0.025"), "output": Decimal("2.00")},
        "gpt-5-nano": {"input": Decimal("0.05"), "cached_input": Decimal("0.005"), "output": Decimal("0.40")},
    }.get(model, {"input": Decimal("0.25"), "cached_input": Decimal("0.025"), "output": Decimal("2.00")})
    input_tokens = max(int(usage.get("input_tokens") or 0) - int(usage.get("cached_input_tokens") or 0), 0)
    cached_tokens = int(usage.get("cached_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cost = (
        Decimal(input_tokens) * pricing["input"]
        + Decimal(cached_tokens) * pricing["cached_input"]
        + Decimal(output_tokens) * pricing["output"]
    ) / Decimal("1000000")
    return cost.quantize(Decimal("0.0001"))
