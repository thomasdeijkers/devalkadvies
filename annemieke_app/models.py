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
    confidence: Mapped[int] = mapped_column(Integer, default=50)
    raw_text: Mapped[str] = mapped_column(Text, default="")

    document: Mapped[IncomingDocument] = relationship(back_populates="budget_lines")
