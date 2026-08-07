from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class EmailDraft(Base):
    __tablename__ = "email_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    contact_id: Mapped[str] = mapped_column(String(36), ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    sending_mailbox_id: Mapped[str | None] = mapped_column(String(64))
    custom_instructions: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    user_prompt: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    # What the draft was personalised on: the verified activity it opened with, so the sender
    # can see the evidence behind "congratulations on ..." before it goes out.
    personalization: Mapped[dict | None] = mapped_column(JSON)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    contact: Mapped["Contact"] = relationship(back_populates="email_drafts")


class DraftRun(Base):
    """One bulk drafting job, tracked so the browser never has to hold the request open.

    Writing forty emails takes minutes, and Azure App Service closes any connection that has
    been idle for 230 seconds — which surfaced in the UI as "Failed to fetch" on exactly the
    large batches the objective flow is designed to produce. The work now runs detached and the
    page polls this row, so batch size and request lifetime are unrelated.
    """

    __tablename__ = "draft_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    # What is being worked on right now, for the progress line.
    current_label: Mapped[str | None] = mapped_column(String(320))
    phase: Mapped[str] = mapped_column(String(24), default="studying")
    objective: Mapped[str | None] = mapped_column(Text)
    custom_instructions: Mapped[str | None] = mapped_column(Text)
    contact_ids: Mapped[list | None] = mapped_column(JSON)
    mailbox_ids: Mapped[list | None] = mapped_column(JSON)
    draft_ids: Mapped[list | None] = mapped_column(JSON)
    errors: Mapped[list | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class OutreachPrompt(Base):
    __tablename__ = "outreach_prompts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "default")
    system_prompt: Mapped[str] = mapped_column(Text)
    user_prompt_template: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


from app.models.contact import Contact  # noqa: E402
