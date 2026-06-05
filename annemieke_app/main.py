import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session, aliased

from .config import settings
from .database import SessionLocal, create_db, engine, get_session
from .exporter import budget_document_to_xlsx, control_model_document_to_xlsx, selected_budget_lines_to_xlsx
from .index_provider import sync_price_index_series
from .kengetallen import import_reference_lines
from .models import (
    AssessmentTemplate,
    BudgetLine,
    ExtractedField,
    IncomingDocument,
    NormalizationTerm,
    OpenAIUsageEvent,
    PriceIndexSeries,
    Project,
    ReferenceDataset,
    ReferenceLine,
    Relation,
    ScheduledJob,
)
from .normalizer import (
    apply_normalization,
    is_noise_line,
    normalization_key,
    seed_default_normalization_terms,
    split_aliases,
)
from .parser import parse_pdf
from .template_exporter import fill_screening_template


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
    with SessionLocal() as session:
        if seed_default_normalization_terms(session):
            _reapply_all_normalization(session)


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


@app.get("/kengetallen", response_class=HTMLResponse)
def reference_data_page(
    request: Request,
    q: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return _render_workspace(request, session, "kengetallen", query=q)


@app.get("/beoordeling", response_class=HTMLResponse)
def assessment_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _render_workspace(request, session, "beoordeling")


@app.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _render_workspace(request, session, "templates")


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


@app.get("/normalisatie", response_class=HTMLResponse)
def normalization_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _render_workspace(request, session, "normalisatie")


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
    reference_datasets = session.scalars(
        select(ReferenceDataset).order_by(ReferenceDataset.created_at.desc()).limit(20)
    ).all()
    assessment_templates = session.scalars(
        select(AssessmentTemplate).order_by(AssessmentTemplate.created_at.desc()).limit(20)
    ).all()
    reference_line_count = session.scalar(select(func.count(ReferenceLine.id))) or 0
    reference_lines = _reference_lines(session, query) if active_page == "kengetallen" else []
    assessment_documents = session.scalars(
        select(IncomingDocument)
        .where(IncomingDocument.document_type == "begroting_beoordeling")
        .order_by(IncomingDocument.created_at.desc())
        .limit(30)
    ).all()
    normalization_terms = session.scalars(
        select(NormalizationTerm).order_by(NormalizationTerm.canonical_label, NormalizationTerm.alias).limit(400)
    ).all()
    normalization_candidates = _normalization_candidate_lines(session) if active_page == "normalisatie" else []
    normalization_candidate_groups = _normalization_candidate_groups(normalization_candidates)
    normalization_stats = _normalization_stats(session)
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
            "reference_datasets": reference_datasets,
            "reference_lines": reference_lines,
            "assessment_templates": assessment_templates,
            "assessment_documents": assessment_documents,
            "reference_line_count": reference_line_count,
            "normalization_terms": normalization_terms,
            "normalization_candidates": normalization_candidates,
            "normalization_candidate_groups": normalization_candidate_groups,
            "normalization_stats": normalization_stats,
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
    return FileResponse(
        file_path,
        media_type="application/pdf",
        filename=document.original_filename,
        content_disposition_type="inline",
    )


@app.post("/documents", response_class=HTMLResponse)
async def upload_document(
    background_tasks: BackgroundTasks,
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

    selected_project = session.get(Project, _int_or_none(project_id)) if _int_or_none(project_id) else None
    document = IncomingDocument(
        original_filename=file.filename,
        stored_filename=stored_filename,
        project_id=selected_project.id if selected_project else None,
        project_name=project_name.strip() or None,
        status="processing",
        source="upload",
        parsed_text="",
        parser_notes="Upload ontvangen. Parser loopt op de achtergrond.",
        parser_stage="Upload ontvangen",
        parser_progress=5,
    )

    session.add(document)
    session.commit()
    background_tasks.add_task(_parse_document_background, document.id, "budget_upload")

    return RedirectResponse(f"/documents/{document.id}", status_code=303)


@app.post("/kengetallen/upload")
async def upload_reference_dataset(
    name: str = Form(...),
    source: str = Form(""),
    notes: str = Form(""),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".xlsm"}:
        raise HTTPException(status_code=400, detail="Upload een Excelbestand (.xlsx of .xlsm).")

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{uuid4().hex}{suffix}"
    target_path = settings.upload_dir / stored_filename
    target_path.write_bytes(await file.read())
    imported_lines = import_reference_lines(target_path)

    dataset = ReferenceDataset(
        name=name.strip(),
        source=source.strip() or None,
        notes=notes.strip() or None,
        original_filename=file.filename,
        stored_filename=stored_filename,
        status="active",
    )
    dataset.lines = [
        ReferenceLine(
            line_number=line.line_number,
            regel_type=line.regel_type,
            niveau=line.niveau,
            hoofdstuk_code=line.hoofdstuk_code,
            hoofdstuk_omschrijving=line.hoofdstuk_omschrijving,
            post_code=line.post_code,
            project_name=line.project_name,
            relation_name=line.relation_name,
            document_date=line.document_date,
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
            bron_pagina=line.bron_pagina,
            confidence=line.confidence,
            raw_text=line.raw_text,
        )
        for line in imported_lines
        if not is_noise_line(line.omschrijving_werkzaamheden)
    ]
    apply_normalization(session, dataset.lines)
    session.add(dataset)
    session.commit()
    return RedirectResponse("/kengetallen", status_code=303)


@app.post("/templates")
async def create_assessment_template(
    name: str = Form(...),
    version: str = Form(""),
    description: str = Form(""),
    required_columns: str = Form(""),
    output_filename: str = Form(""),
    target_sheet: str = Form(""),
    file: UploadFile | None = File(None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    default_columns = (
        "omschrijving_werkzaamheden,hoeveelheid,eenheid,norm_arbeid,uren,materiaal,materieel,"
        "onderaannemer,eenheidsprijs,totaal_prijs_per_regel"
    )
    stored_filename = None
    original_filename = None
    if file and file.filename:
        suffix = Path(file.filename).suffix.lower()
        if suffix not in {".xlsx", ".xlsm"}:
            raise HTTPException(status_code=400, detail="Upload een Excel template (.xlsx of .xlsm).")
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        stored_filename = f"{uuid4().hex}{suffix}"
        (settings.upload_dir / stored_filename).write_bytes(await file.read())
        original_filename = file.filename

    session.add(
        AssessmentTemplate(
            name=name.strip(),
            version=version.strip() or None,
            original_filename=original_filename,
            stored_filename=stored_filename,
            target_sheet=target_sheet.strip() or None,
            description=description.strip() or None,
            required_columns=required_columns.strip() or default_columns,
            output_filename=output_filename.strip() or None,
        )
    )
    session.commit()
    return RedirectResponse("/templates", status_code=303)


@app.post("/kengetallen/{dataset_id}/delete")
def delete_reference_dataset(dataset_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    dataset = session.get(ReferenceDataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Kengetallendataset niet gevonden.")

    file_path = settings.upload_dir / dataset.stored_filename if dataset.stored_filename else None
    session.delete(dataset)
    session.commit()
    if file_path and file_path.exists():
        file_path.unlink()
    return RedirectResponse("/kengetallen", status_code=303)


@app.post("/normalisatie/terms")
def create_normalization_term(
    canonical_label: str = Form(...),
    aliases: str = Form(""),
    category: str = Form("omschrijving"),
    match_type: str = Form("fuzzy"),
    min_score: str = Form("82"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    _add_normalization_terms(session, canonical_label, aliases, category, match_type, min_score)
    _reapply_all_normalization(session)
    return RedirectResponse("/normalisatie", status_code=303)


@app.post("/normalisatie/terms/{term_id}")
def update_normalization_term(
    term_id: int,
    canonical_label: str = Form(...),
    alias: str = Form(...),
    category: str = Form("omschrijving"),
    match_type: str = Form("fuzzy"),
    min_score: str = Form("82"),
    active: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    term = session.get(NormalizationTerm, term_id)
    if term is None:
        raise HTTPException(status_code=404, detail="Normalisatieterm niet gevonden.")
    label = canonical_label.strip()
    alias_value = alias.strip()
    if not label or not alias_value:
        raise HTTPException(status_code=400, detail="Vul standaardterm en synoniem in.")
    term.canonical_label = label
    term.canonical_key = normalization_key(label) or label.lower().replace(" ", "_")[:180]
    term.alias = alias_value
    term.category = category.strip() or "omschrijving"
    term.match_type = match_type if match_type in {"hard", "fuzzy"} else "fuzzy"
    term.min_score = max(50, min(100, _int_or_none(min_score) or 82))
    term.active = 1 if active else 0
    _reapply_all_normalization(session)
    return RedirectResponse("/normalisatie", status_code=303)


@app.post("/normalisatie/terms/{term_id}/delete")
def delete_normalization_term(term_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    term = session.get(NormalizationTerm, term_id)
    if term is None:
        raise HTTPException(status_code=404, detail="Normalisatieterm niet gevonden.")
    session.delete(term)
    _reapply_all_normalization(session)
    return RedirectResponse("/normalisatie", status_code=303)


@app.post("/normalisatie/apply")
def apply_normalization_to_existing(session: Session = Depends(get_session)) -> RedirectResponse:
    _reapply_all_normalization(session)
    return RedirectResponse("/normalisatie", status_code=303)


@app.post("/normalisatie/suggesties/{line_id}/validate")
def validate_normalization_suggestion(line_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    line = session.get(BudgetLine, line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="Voorstel niet gevonden.")
    canonical_label = (line.normalization_candidate or line.normalized_omschrijving or line.omschrijving_werkzaamheden).strip()
    if not canonical_label:
        raise HTTPException(status_code=400, detail="Geen voorstel beschikbaar.")
    _add_normalization_terms(
        session,
        canonical_label,
        line.omschrijving_werkzaamheden,
        "omschrijving",
        "hard",
        "100",
    )
    _reapply_all_normalization(session)
    return RedirectResponse("/normalisatie", status_code=303)


@app.post("/normalisatie/suggesties/{line_id}/terms")
def create_term_from_suggestion(
    line_id: int,
    canonical_label: str = Form(...),
    aliases: str = Form(""),
    category: str = Form("omschrijving"),
    match_type: str = Form("fuzzy"),
    min_score: str = Form("82"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    line = session.get(BudgetLine, line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="Voorstel niet gevonden.")
    alias_values = aliases
    if line.omschrijving_werkzaamheden and line.omschrijving_werkzaamheden not in alias_values:
        alias_values = f"{line.omschrijving_werkzaamheden}\n{alias_values}".strip()
    _add_normalization_terms(session, canonical_label, alias_values, category, match_type, min_score)
    _reapply_all_normalization(session)
    return RedirectResponse("/normalisatie", status_code=303)


@app.post("/beoordeling/upload")
async def upload_assessment_input(
    background_tasks: BackgroundTasks,
    project_name: str = Form(""),
    project_id: str = Form(""),
    template_id: str = Form(""),
    reference_dataset_id: str = Form(""),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".xlsx", ".xlsm"}:
        raise HTTPException(status_code=400, detail="Upload een PDF of Excelbestand.")

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{uuid4().hex}{suffix}"
    target_path = settings.upload_dir / stored_filename
    target_path.write_bytes(await file.read())
    selected_project = session.get(Project, _int_or_none(project_id)) if _int_or_none(project_id) else None

    document = IncomingDocument(
        original_filename=file.filename or "begroting",
        stored_filename=stored_filename,
        project_id=selected_project.id if selected_project else None,
        project_name=project_name.strip() or None,
        status="processing" if suffix == ".pdf" else "needs_review",
        document_type="begroting_beoordeling",
        source="excel" if suffix in {".xlsx", ".xlsm"} else "pdf",
        parser_notes=(
            f"{_assessment_note(template_id, reference_dataset_id)} | Upload ontvangen. Parser loopt op de achtergrond."
            if suffix == ".pdf"
            else _assessment_note(template_id, reference_dataset_id)
        ),
        parser_stage="Upload ontvangen" if suffix == ".pdf" else "Excel verwerkt",
        parser_progress=5 if suffix == ".pdf" else 100,
    )
    if suffix == ".pdf":
        session.add(document)
        session.commit()
        background_tasks.add_task(_parse_document_background, document.id, "assessment_upload")
        return RedirectResponse(f"/documents/{document.id}", status_code=303)
    else:
        imported_lines = import_reference_lines(target_path)
        document.parsed_text = "\n".join(line.raw_text for line in imported_lines[:500])
        document.budget_lines = [
            BudgetLine(
                line_number=line.line_number,
                regel_type=line.regel_type,
                niveau=line.niveau,
                hoofdstuk_code=line.hoofdstuk_code,
                hoofdstuk_omschrijving=line.hoofdstuk_omschrijving,
                post_code=line.post_code,
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
                bron_pagina=line.bron_pagina,
                confidence=line.confidence,
                raw_text=line.raw_text,
            )
            for line in imported_lines
            if not is_noise_line(line.omschrijving_werkzaamheden)
        ]

    for line in document.budget_lines:
        _normalize_line_prices(line)
    apply_normalization(session, document.budget_lines)
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
    background_tasks: BackgroundTasks,
    document_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    document.status = "processing"
    document.parser_notes = "OpenAI herparse gestart. Parser loopt op de achtergrond."
    document.parser_stage = "OpenAI herparse gestart"
    document.parser_progress = 5
    session.commit()
    background_tasks.add_task(_parse_document_background, document.id, "budget_reparse", True)
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
            "normalization_suggestions": [
                line for line in document.budget_lines if line.normalization_candidate and line.normalization_score < 100
            ][:12],
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
    return_to: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")
    if status not in {"needs_review", "processed", "archived"}:
        raise HTTPException(status_code=400, detail="Onbekende status.")

    document.status = status
    session.commit()
    target = _safe_return(return_to) or request_url_for_document(document.id, archived=status == "archived")
    return RedirectResponse(target, status_code=303)


@app.post("/documents/{document_id}/delete")
def delete_document(
    document_id: int,
    return_to: str = Form(""),
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
    return RedirectResponse(_safe_return(return_to) or "/", status_code=303)


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
    apply_normalization(session, [line])
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
        apply_normalization(session, [line])

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
    apply_normalization(session, [line])
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


@app.get("/documents/{document_id}/controlemodel.xlsx")
def export_control_model(document_id: int, session: Session = Depends(get_session)) -> StreamingResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    stream = control_model_document_to_xlsx(document)
    filename = f"controlemodel-{document.id}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/documents/{document_id}/screening.xlsx")
def export_screening_template(document_id: int, session: Session = Depends(get_session)) -> StreamingResponse:
    document = session.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document niet gevonden.")

    template = _template_for_document(session, document)
    if template and template.stored_filename:
        template_path = settings.upload_dir / template.stored_filename
        if template_path.exists():
            stream = fill_screening_template(document, template_path, template.target_sheet)
            filename = template.output_filename or f"screening-{document.id}.xlsx"
            return StreamingResponse(
                stream,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

    stream = control_model_document_to_xlsx(document)
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="screening-fallback-{document.id}.xlsx"'},
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
        "parser_stage": document.parser_stage or "",
        "parser_progress": document.parser_progress or 0,
        "parser_notes": document.parser_notes or "",
        "line_count": len(document.budget_lines),
        "parsed_text_available": bool(document.parsed_text),
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
                BudgetLine.normalized_omschrijving.ilike(search),
                BudgetLine.normalized_key.ilike(search),
                BudgetLine.normalization_candidate.ilike(search),
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


def _reference_lines(session: Session, query: str | None, limit: int = 300) -> list[ReferenceLine]:
    statement = select(ReferenceLine).join(ReferenceLine.dataset)
    cleaned_query = (query or "").strip()
    if cleaned_query:
        search = f"%{cleaned_query}%"
        statement = statement.where(
            or_(
                ReferenceDataset.name.ilike(search),
                ReferenceDataset.source.ilike(search),
                ReferenceLine.project_name.ilike(search),
                ReferenceLine.relation_name.ilike(search),
                ReferenceLine.omschrijving_werkzaamheden.ilike(search),
                ReferenceLine.normalized_omschrijving.ilike(search),
                ReferenceLine.normalized_key.ilike(search),
                ReferenceLine.eenheid.ilike(search),
            )
        )
    return session.scalars(
        statement.order_by(ReferenceDataset.created_at.desc(), ReferenceLine.line_number).limit(limit)
    ).unique().all()


def _normalization_candidate_lines(session: Session, limit: int = 120) -> list[BudgetLine]:
    return session.scalars(
        select(BudgetLine)
        .join(BudgetLine.document)
        .where(
            BudgetLine.normalization_score < 100,
            or_(
                BudgetLine.normalization_candidate.is_not(None),
                and_(
                    BudgetLine.normalization_method.in_(["raw", "reference"]),
                    BudgetLine.eenheidsprijs.is_not(None),
                    BudgetLine.omschrijving_werkzaamheden != "",
                ),
            ),
        )
        .order_by(BudgetLine.normalization_candidate.is_(None), BudgetLine.normalization_score.desc(), BudgetLine.id.desc())
        .limit(limit)
    ).unique().all()


def _normalization_candidate_groups(lines: list[BudgetLine]) -> list[dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}
    for line in lines:
        label = (
            line.normalization_candidate
            or line.normalized_omschrijving
            or line.omschrijving_werkzaamheden
            or "Nieuwe term nodig"
        ).strip()
        key = normalization_key(label) or label.lower()
        group = groups.setdefault(
            key,
            {
                "label": label,
                "lines": [],
                "best_score": 0,
                "methods": set(),
            },
        )
        group["lines"].append(line)
        group["best_score"] = max(int(group["best_score"]), int(line.normalization_score or 0))
        if line.normalization_method:
            group["methods"].add(line.normalization_method)

    grouped = []
    for group in groups.values():
        group_lines = sorted(
            group["lines"],
            key=lambda item: (-(item.normalization_score or 0), item.line_number, item.id),
        )
        methods = sorted(str(method) for method in group["methods"])
        grouped.append(
            {
                "label": group["label"],
                "lines": group_lines,
                "best_score": group["best_score"],
                "method_label": ", ".join(methods) if methods else "raw",
                "count": len(group_lines),
            }
        )
    return sorted(grouped, key=lambda item: (-int(item["best_score"]), str(item["label"]).lower()))


def _normalization_stats(session: Session) -> dict[str, int]:
    budget_total = session.scalar(select(func.count(BudgetLine.id))) or 0
    reference_total = session.scalar(select(func.count(ReferenceLine.id))) or 0
    term_count = session.scalar(select(func.count(NormalizationTerm.id))) or 0
    suggestions = session.scalar(
        select(func.count(BudgetLine.id)).where(BudgetLine.normalization_candidate.is_not(None))
    ) or 0
    hard_matches = session.scalar(
        select(func.count(BudgetLine.id)).where(BudgetLine.normalization_method == "hard")
    ) or 0
    fuzzy_matches = session.scalar(
        select(func.count(BudgetLine.id)).where(BudgetLine.normalization_method.in_(["fuzzy", "reference"]))
    ) or 0
    raw_priced = session.scalar(
        select(func.count(BudgetLine.id)).where(
            BudgetLine.normalization_method.in_(["raw", "reference"]),
            BudgetLine.eenheidsprijs.is_not(None),
            BudgetLine.normalization_score < 100,
        )
    ) or 0
    return {
        "budget_total": int(budget_total),
        "reference_total": int(reference_total),
        "term_count": int(term_count),
        "suggestions": int(suggestions) + int(raw_priced),
        "hard_matches": int(hard_matches),
        "fuzzy_matches": int(fuzzy_matches),
    }


def _add_normalization_terms(
    session: Session,
    canonical_label: str,
    aliases: str,
    category: str,
    match_type: str,
    min_score: str,
) -> list[NormalizationTerm]:
    label = canonical_label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Vul een standaardomschrijving in.")
    term_type = match_type if match_type in {"hard", "fuzzy"} else "fuzzy"
    score = max(50, min(100, _int_or_none(min_score) or 82))
    alias_values = split_aliases(aliases) or [label]
    canonical_key = normalization_key(label) or label.lower().replace(" ", "_")[:180]
    existing = {
        (term.canonical_key, normalization_key(term.alias))
        for term in session.scalars(select(NormalizationTerm)).all()
    }
    added: list[NormalizationTerm] = []
    for alias in alias_values:
        lookup = (canonical_key, normalization_key(alias))
        if lookup in existing:
            continue
        term = NormalizationTerm(
            canonical_key=canonical_key,
            canonical_label=label,
            alias=alias.strip(),
            category=category.strip() or "omschrijving",
            match_type=term_type,
            min_score=score,
            active=1,
        )
        session.add(term)
        added.append(term)
        existing.add(lookup)
    return added


def _reapply_all_normalization(session: Session) -> None:
    reference_lines = session.scalars(select(ReferenceLine)).all()
    apply_normalization(session, reference_lines)
    session.flush()

    budget_lines = session.scalars(select(BudgetLine)).all()
    for line in budget_lines:
        _normalize_line_prices(line)
    apply_normalization(session, budget_lines)
    session.commit()


def _parse_document_background(document_id: int, usage_source: str, force_openai: bool = False) -> None:
    session = SessionLocal()
    try:
        document = session.get(IncomingDocument, document_id)
        if document is None:
            return
        _set_parse_progress(session, document, 12, "Bestand voorbereiden")
        file_path = settings.upload_dir / document.stored_filename
        if not file_path.exists():
            document.status = "parse_error"
            document.parser_notes = "Parser fout: bestand niet gevonden op de server."
            document.parser_stage = "Bestand niet gevonden"
            document.parser_progress = 100
            session.commit()
            return

        _set_parse_progress(session, document, 28, "PDF lezen, OCR/OpenAI voorbereiden")
        parsed = parse_pdf(file_path, force_openai=force_openai)
        _set_parse_progress(session, document, 76, f"{len(parsed.budget_lines)} regels herkend")
        document.source = parsed.parse_method
        document.parsed_text = parsed.text
        document.parser_notes = _merge_parser_notes(document.parser_notes, _parser_note(parsed))
        document.fields = [
            ExtractedField(field_name=field.name, field_value=field.value, confidence=field.confidence)
            for field in parsed.fields
        ]
        _set_parse_progress(session, document, 88, "Begrotingsregels opslaan")
        document.budget_lines = [
            _budget_line_from_parsed(line)
            for line in parsed.budget_lines
            if not is_noise_line(line.omschrijving_werkzaamheden)
        ]
        for line in document.budget_lines:
            _normalize_line_prices(line)
        apply_normalization(session, document.budget_lines)
        document.status = "needs_review"
        document.parser_stage = "Klaar voor controle"
        document.parser_progress = 100
        session.commit()
        _record_openai_usage(session, usage_source, document.id, parsed.openai_usage)
    except Exception as exc:
        session.rollback()
        document = session.get(IncomingDocument, document_id)
        if document is not None:
            document.status = "parse_error"
            document.parser_notes = f"Parser fout: {str(exc)[:500]}"
            document.parser_stage = "Parser fout"
            document.parser_progress = 100
            session.commit()
    finally:
        session.close()


def _set_parse_progress(session: Session, document: IncomingDocument, progress: int, stage: str) -> None:
    document.status = "processing"
    document.parser_progress = max(0, min(100, progress))
    document.parser_stage = stage
    session.commit()


def _budget_line_from_parsed(line) -> BudgetLine:
    return BudgetLine(
        line_number=line.line_number,
        regel_type=line.regel_type,
        niveau=line.niveau,
        hoofdstuk_code=line.hoofdstuk_code,
        hoofdstuk_omschrijving=line.hoofdstuk_omschrijving,
        post_code=line.post_code,
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
        bron_pagina=line.bron_pagina,
        confidence=line.confidence,
        raw_text=line.raw_text,
    )


def _merge_parser_notes(existing: str | None, parsed_note: str | None) -> str | None:
    keep = []
    for part in (existing or "").split("|"):
        cleaned = part.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if "parser loopt" in lowered or "upload ontvangen" in lowered or "herparse gestart" in lowered:
            continue
        keep.append(cleaned)
    if parsed_note:
        keep.append(parsed_note)
    return " | ".join(dict.fromkeys(keep)) if keep else None


def _latest_template(session: Session) -> AssessmentTemplate | None:
    return session.scalars(
        select(AssessmentTemplate)
        .where(AssessmentTemplate.status == "active")
        .order_by(AssessmentTemplate.created_at.desc())
        .limit(1)
    ).first()


def _template_for_document(session: Session, document: IncomingDocument) -> AssessmentTemplate | None:
    for part in (document.parser_notes or "").split("|"):
        key, _, value = part.strip().partition("=")
        if key == "template_id":
            template_id = _int_or_none(value)
            if template_id:
                template = session.get(AssessmentTemplate, template_id)
                if template is not None:
                    return template
    return _latest_template(session)


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


def _safe_return(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if cleaned.startswith("/") and not cleaned.startswith("//"):
        return cleaned
    return None


def _parser_note(parsed) -> str | None:
    parts = [f"Parse methode: {parsed.parse_method}", f"zekerheid: {parsed.confidence}%"]
    if parsed.openai_usage:
        parts.append(f"OpenAI tokens: {parsed.openai_usage.get('total_tokens', 0)}")
    if parsed.notes:
        parts.append(parsed.notes)
    return " | ".join(parts)


def _assessment_note(template_id: str, reference_dataset_id: str) -> str:
    parts = ["Module: begroting beoordeling"]
    if template_id:
        parts.append(f"template_id={template_id}")
    if reference_dataset_id:
        parts.append(f"kengetallen_dataset_id={reference_dataset_id}")
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
