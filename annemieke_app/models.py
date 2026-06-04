from datetime import datetime

from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class IncomingDocument(Base):
    __tablename__ = "incoming_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    project_name: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), default="new", index=True)
    document_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    parsed_text: Mapped[str] = mapped_column(Text, default="")
    parser_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    fields: Mapped[list["ExtractedField"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="ExtractedField.field_name",
    )
    budget_lines: Mapped[list["BudgetLine"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="BudgetLine.line_number",
    )
    project: Mapped["Project | None"] = relationship(back_populates="documents")


class Relation(Base):
    __tablename__ = "relations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    relation_type: Mapped[str] = mapped_column(String(60), default="opdrachtgever", index=True)
    name: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    contact_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    address: Mapped[str | None] = mapped_column(String(180), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(30), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    email: Mapped[str | None] = mapped_column(String(180), nullable=True)
    website: Mapped[str | None] = mapped_column(String(180), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    projects_as_client: Mapped[list["Project"]] = relationship(
        back_populates="client",
        foreign_keys="Project.client_relation_id",
    )
    projects_as_architect: Mapped[list["Project"]] = relationship(
        back_populates="architect",
        foreign_keys="Project.architect_relation_id",
    )
    projects_as_constructor: Mapped[list["Project"]] = relationship(
        back_populates="constructor",
        foreign_keys="Project.constructor_relation_id",
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_number: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(220), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(180), nullable=True)
    status: Mapped[str] = mapped_column(String(60), default="actief", index=True)
    client_relation_id: Mapped[int | None] = mapped_column(ForeignKey("relations.id"), nullable=True, index=True)
    architect_relation_id: Mapped[int | None] = mapped_column(ForeignKey("relations.id"), nullable=True)
    constructor_relation_id: Mapped[int | None] = mapped_column(ForeignKey("relations.id"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    client: Mapped[Relation | None] = relationship(foreign_keys=[client_relation_id], back_populates="projects_as_client")
    architect: Mapped[Relation | None] = relationship(foreign_keys=[architect_relation_id], back_populates="projects_as_architect")
    constructor: Mapped[Relation | None] = relationship(foreign_keys=[constructor_relation_id], back_populates="projects_as_constructor")
    documents: Mapped[list[IncomingDocument]] = relationship(back_populates="project")


class ExtractedField(Base):
    __tablename__ = "extracted_fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("incoming_documents.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    field_value: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[int] = mapped_column(Integer, default=50)
    source: Mapped[str] = mapped_column(String(40), default="parser")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[IncomingDocument] = relationship(back_populates="fields")


class BudgetLine(Base):
    __tablename__ = "budget_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("incoming_documents.id"), index=True)
    line_number: Mapped[int] = mapped_column(Integer, default=0)
    omschrijving_werkzaamheden: Mapped[str] = mapped_column(Text, default="")
    hoeveelheid: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    eenheid: Mapped[str | None] = mapped_column(String(40), nullable=True)
    norm_arbeid: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    uren: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    materiaal: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    materieel: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    onderaannemer: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    totaal_prijs_per_regel: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    eenheidsprijs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    price_index_series_id: Mapped[int | None] = mapped_column(ForeignKey("price_index_series.id"), nullable=True, index=True)
    base_price_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    indexed_eenheidsprijs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    indexed_totaal_prijs_per_regel: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, default=50)
    raw_text: Mapped[str] = mapped_column(Text, default="")

    document: Mapped[IncomingDocument] = relationship(back_populates="budget_lines")
    price_index_series: Mapped["PriceIndexSeries | None"] = relationship(back_populates="budget_lines")


class PriceIndexSeries(Base):
    __tablename__ = "price_index_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(180), nullable=True)
    provider: Mapped[str] = mapped_column(String(60), default="manual", index=True)
    api_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_field: Mapped[str | None] = mapped_column(String(120), nullable=True)
    value_field: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    base_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    values: Mapped[list["PriceIndexValue"]] = relationship(
        back_populates="series",
        cascade="all, delete-orphan",
        order_by="PriceIndexValue.effective_date",
    )
    budget_lines: Mapped[list["BudgetLine"]] = relationship(back_populates="price_index_series")


class PriceIndexValue(Base):
    __tablename__ = "price_index_values"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("price_index_series.id"), index=True)
    effective_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    index_value: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    series: Mapped["PriceIndexSeries"] = relationship(back_populates="values")


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(80), default="index_sync", index=True)
    cron_expression: Mapped[str] = mapped_column(String(80), default="0 5 1 * *")
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class OpenAIUsageEvent(Base):
    __tablename__ = "openai_usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(80), default="budget_parse", index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(120), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
