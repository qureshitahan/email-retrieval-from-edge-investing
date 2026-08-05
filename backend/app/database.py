from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import BASE_DIR, get_settings

# Set by App Service. On Azure the persistent path (/home) is an Azure Files SMB share, which
# changes what SQLite settings are safe - see the journal-mode note below.
ON_AZURE_APP_SERVICE = bool(os.getenv("WEBSITE_SITE_NAME"))

settings = get_settings()
data_dir = BASE_DIR / "data"
data_dir.mkdir(parents=True, exist_ok=True)

if settings.database_url.startswith("sqlite"):
    # Ensure the SQLite file's parent directory exists (e.g. /home/data on Azure)
    db_path = settings.database_url.split("sqlite:///", 1)[-1]
    if db_path and db_path not in (":memory:",):
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

IS_SQLITE = settings.database_url.startswith("sqlite")

# How long a statement waits for another connection's lock before giving up. SQLite's default
# via python-sqlite3 is 5 seconds, which a sync writing to Azure Files (an SMB share, where
# every write is a network round trip) blows straight through - readers then die with
# "database is locked" instead of simply waiting their turn.
SQLITE_BUSY_TIMEOUT_SECONDS = 30

connect_args = (
    {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS} if IS_SQLITE else {}
)
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# Set once, on the first connection, and reported so the startup log says which mode won.
journal_mode_in_use: str | None = None


if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        global journal_mode_in_use
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            # The timeout above covers python-sqlite3; this covers statements SQLite runs
            # internally on the same connection.
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")

            # WAL lets readers work while a writer is mid-transaction, which is exactly the
            # contention a long sync creates - but it relies on shared memory and SQLite
            # documents that it does NOT work over a network filesystem. On Azure App Service
            # the database lives on /home, an Azure Files SMB share, so asking for WAL there
            # risks disk I/O errors against real data. Detect that environment and leave the
            # journal alone; the busy timeout above is what keeps reads alive on Azure.
            if ON_AZURE_APP_SERVICE:
                journal_mode_in_use = "delete (WAL skipped: /home is a network share)"
            else:
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    row = cursor.fetchone()
                    journal_mode_in_use = (row[0] if row else "unknown").lower()
                except Exception:
                    journal_mode_in_use = "delete (WAL refused)"

            # FULL forces an fsync per commit; over SMB that dominates the write time. NORMAL
            # is durable against process crashes, and only risks the last commit on a host
            # power failure - an acceptable trade for mail that can simply be re-synced.
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def _assign_contact_list_numbers(conn) -> None:
    from sqlalchemy import text

    rows = conn.execute(
        text(
            """
            SELECT id FROM contacts
            WHERE is_internal = 0 AND is_excluded = 0
            ORDER BY fundraising_relevance_score DESC,
                     CASE WHEN last_contacted_at IS NULL THEN 1 ELSE 0 END,
                     last_contacted_at DESC,
                     primary_email ASC
            """
        )
    ).fetchall()
    for index, (contact_id,) in enumerate(rows, start=1):
        conn.execute(
            text("UPDATE contacts SET list_number = :num WHERE id = :id"),
            {"num": index, "id": contact_id},
        )


def run_migrations() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "contacts" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("contacts")}
    with engine.begin() as conn:
        if "review_status" not in columns:
            conn.execute(
                text("ALTER TABLE contacts ADD COLUMN review_status VARCHAR(16) DEFAULT 'pending'")
            )
        if "list_number" not in columns:
            conn.execute(text("ALTER TABLE contacts ADD COLUMN list_number INTEGER"))

        needs_numbers = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM contacts
                WHERE list_number IS NULL AND is_internal = 0 AND is_excluded = 0
                """
            )
        ).scalar()
        if needs_numbers:
            _assign_contact_list_numbers(conn)

    if "email_messages" in inspector.get_table_names():
        msg_columns = {column["name"] for column in inspector.get_columns("email_messages")}
        with engine.begin() as conn:
            if "direction" not in msg_columns:
                conn.execute(
                    text("ALTER TABLE email_messages ADD COLUMN direction VARCHAR(16) DEFAULT 'outbound'")
                )
            if "mailbox_id" not in msg_columns:
                conn.execute(text("ALTER TABLE email_messages ADD COLUMN mailbox_id VARCHAR(64)"))
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_email_messages_mailbox_id "
                         "ON email_messages (mailbox_id)")
                )

    if "sync_runs" in inspector.get_table_names():
        run_columns = {column["name"] for column in inspector.get_columns("sync_runs")}
        with engine.begin() as conn:
            if "mailbox_id" not in run_columns:
                conn.execute(text("ALTER TABLE sync_runs ADD COLUMN mailbox_id VARCHAR(64)"))
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_sync_runs_mailbox_id "
                         "ON sync_runs (mailbox_id)")
                )

    if "contact_context" in inspector.get_table_names():
        ctx_columns = {column["name"] for column in inspector.get_columns("contact_context")}
        with engine.begin() as conn:
            if "ai_relationship_context" not in ctx_columns:
                conn.execute(text("ALTER TABLE contact_context ADD COLUMN ai_relationship_context TEXT"))

    if "email_drafts" in inspector.get_table_names():
        draft_columns = {column["name"] for column in inspector.get_columns("email_drafts")}
        with engine.begin() as conn:
            if "sending_mailbox_id" not in draft_columns:
                conn.execute(text("ALTER TABLE email_drafts ADD COLUMN sending_mailbox_id VARCHAR(64)"))

    if "contacts" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("contacts")}
        with engine.begin() as conn:
            if "last_inbound_at" not in columns:
                conn.execute(text("ALTER TABLE contacts ADD COLUMN last_inbound_at DATETIME"))
            if "last_outbound_at" not in columns:
                conn.execute(text("ALTER TABLE contacts ADD COLUMN last_outbound_at DATETIME"))
            if "awaiting_reply" not in columns:
                conn.execute(
                    text("ALTER TABLE contacts ADD COLUMN awaiting_reply BOOLEAN DEFAULT 0")
                )
            if "days_since_outreach" not in columns:
                conn.execute(text("ALTER TABLE contacts ADD COLUMN days_since_outreach INTEGER"))


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    run_migrations()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
