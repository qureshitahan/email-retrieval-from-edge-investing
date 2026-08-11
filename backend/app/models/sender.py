"""Who is writing, and what they can credibly claim.

Every other model here describes the *recipient*. These two describe the sender, because a
draft that knows everything about the person it is addressed to and nothing about the person
sending it still opens well and then pitches generically.

A profile belongs to a mailbox: outreach from Galaxy Pharma should make Galaxy's case and sign
as Galaxy, not as Edge Investing, and the mailbox is already the thing a draft is routed to.

Documents are stored as extracted text rather than as the original bytes. The text is what the
proof-point pass and the draft prompt read; keeping the binary as well would mean a file store,
a size budget on Azure Files, and a second copy of the same résumé to keep in sync.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class SenderProfile(Base):
    """The sender's own story, per mailbox: who they are and what they have done."""

    __tablename__ = "sender_profiles"

    # The mailbox id from OUTREACH_MAILBOXES. Not a foreign key: mailboxes live in .env, not in
    # the database, so a profile has to survive one being renamed or temporarily removed.
    mailbox_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(256))
    title: Mapped[str | None] = mapped_column(String(256))
    company: Mapped[str | None] = mapped_column(String(256))
    # One or two sentences on what this person is doing now - the thing the pitch hangs off.
    positioning: Mapped[str | None] = mapped_column(Text)
    linkedin_url: Mapped[str | None] = mapped_column(String(512))
    phone: Mapped[str | None] = mapped_column(String(64))
    website: Mapped[str | None] = mapped_column(String(512))
    signature: Mapped[str | None] = mapped_column(Text)
    # Merged from the documents, then editable by hand. Each entry: {text, source, pinned}.
    proof_points: Mapped[list | None] = mapped_column(JSON)
    keywords: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    documents: Mapped[list["SenderDocument"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class SenderDocument(Base):
    """One uploaded résumé, bio, deal sheet or case study, reduced to text and proof points."""

    __tablename__ = "sender_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    mailbox_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sender_profiles.mailbox_id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(512))
    # resume | bio | deal_sheet | case_study | other - drives nothing yet beyond display, but
    # it is what the reviewer needs to see to know the profile is built from the right things.
    kind: Mapped[str] = mapped_column(String(32), default="other")
    content_text: Mapped[str | None] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    proof_points: Mapped[list | None] = mapped_column(JSON)
    keywords: Mapped[list | None] = mapped_column(JSON)
    summary: Mapped[str | None] = mapped_column(Text)
    # ready | text_only | failed - text_only means it parsed but the proof-point pass did not
    # run or returned nothing, which is worth showing rather than hiding as a silent success.
    status: Mapped[str] = mapped_column(String(16), default="ready")
    error_message: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    profile: Mapped["SenderProfile"] = relationship(back_populates="documents")
