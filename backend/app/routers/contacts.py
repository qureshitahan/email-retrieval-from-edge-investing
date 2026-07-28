from __future__ import annotations

from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.contact import Contact, ContactContext, ContactEmailLink
from app.models.message import EmailMessage
from app.models.sync import SyncRun
from app.schemas import ContactDetail, ContactListItem, ContactUpdate, StatsOut
from app.services.graph_app_client import (
    GRAPH_BASE,
    GraphAppAuthError,
    acquire_app_token,
)
from app.services.graph_client import GraphAuthError, GraphClient
from app.services.mailboxes import (
    PROVIDER_GRAPH,
    PROVIDER_GRAPH_APP,
    MailboxConfigError,
    default_mailbox,
    get_mailbox,
)

router = APIRouter(prefix="/contacts", tags=["contacts"])

REVIEW_STATUSES = {"pending", "approved", "denied"}


def _hydrate_list_item(contact: Contact) -> ContactListItem:
    context: ContactContext | None = contact.context
    latest_message = None
    if contact.email_links:
        latest_message = max(contact.email_links, key=lambda link: link.message.sent_datetime).message
    return ContactListItem(
        id=contact.id,
        list_number=contact.list_number,
        full_name=contact.full_name,
        primary_email=contact.primary_email,
        company_name=contact.company_name,
        company_domain=contact.company_domain,
        first_contacted_at=contact.first_contacted_at,
        last_contacted_at=contact.last_contacted_at,
        email_count=contact.email_count,
        thread_count=contact.thread_count,
        fundraising_relevance_score=contact.fundraising_relevance_score,
        fundraising_relevance_tier=contact.fundraising_relevance_tier,
        contact_type=contact.contact_type,
        status=contact.status,
        review_status=contact.review_status or "pending",
        notes=contact.notes,
        awaiting_reply=contact.awaiting_reply,
        days_since_outreach=contact.days_since_outreach,
        last_inbound_at=contact.last_inbound_at,
        is_internal=contact.is_internal,
        is_personal_email=contact.is_personal_email,
        is_excluded=contact.is_excluded,
        auto_context_short=context.auto_context_short if context else None,
        detected_topics=context.detected_topics if context else None,
        last_subject=latest_message.subject if latest_message else None,
        last_preview=latest_message.body_preview if latest_message else None,
        latest_outlook_weblink=latest_message.outlook_weblink if latest_message else None,
        latest_message_id=latest_message.id if latest_message else None,
        has_ai_summary=bool(context and context.ai_summary),
    )


@router.get("", response_model=dict)
def list_contacts(
    db: Session = Depends(get_db),
    q: str | None = None,
    fundraising_tier: str | None = None,
    exclude_internal: bool = True,
    exclude_personal: bool = False,
    exclude_noise: bool = True,
    email_count_min: int | None = None,
    keyword: str | None = None,
    only_investor: bool = False,
    review_status: str | None = None,
    not_replied_days: int | None = None,
    awaiting_reply_only: bool = False,
    mailbox_id: str | None = None,
    sort: str = "last_contacted_at",
    order: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    query = (
        db.query(Contact)
        .outerjoin(ContactContext, ContactContext.contact_id == Contact.id)
        .options(joinedload(Contact.context), joinedload(Contact.email_links).joinedload(ContactEmailLink.message))
    )

    if exclude_noise:
        query = query.filter(Contact.is_excluded.is_(False))

    # Hide dev-only test seed contacts (graph_message_id starting with test-)
    has_real_message = (
        db.query(ContactEmailLink.id)
        .join(EmailMessage, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(
            ContactEmailLink.contact_id == Contact.id,
            ~EmailMessage.graph_message_id.like("test-%"),
        )
        .exists()
    )
    query = query.filter(has_real_message)

    if exclude_internal:
        query = query.filter(Contact.is_internal.is_(False))
    if exclude_personal:
        query = query.filter(Contact.is_personal_email.is_(False))
    if fundraising_tier:
        tiers = [t.strip() for t in fundraising_tier.split(",") if t.strip()]
        query = query.filter(Contact.fundraising_relevance_tier.in_(tiers))
    if email_count_min is not None:
        query = query.filter(Contact.email_count >= email_count_min)
    if only_investor:
        query = query.filter(or_(Contact.contact_type == "investor", Contact.fundraising_relevance_tier == "high"))
    if review_status:
        query = query.filter(Contact.review_status == review_status)
    if awaiting_reply_only:
        query = query.filter(Contact.awaiting_reply.is_(True))
    if mailbox_id:
        # Contacts stay shared across mailboxes - a person is one relationship. Scope by
        # whether *this* mailbox ever corresponded with them.
        from_mailbox = (
            db.query(ContactEmailLink.id)
            .join(EmailMessage, ContactEmailLink.email_message_id == EmailMessage.id)
            .filter(
                ContactEmailLink.contact_id == Contact.id,
                EmailMessage.mailbox_id == mailbox_id,
            )
            .exists()
        )
        query = query.filter(from_mailbox)
    if not_replied_days is not None:
        query = query.filter(
            Contact.awaiting_reply.is_(True),
            Contact.days_since_outreach.isnot(None),
            Contact.days_since_outreach >= not_replied_days,
        )
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            or_(
                func.lower(Contact.full_name).like(like),
                func.lower(Contact.primary_email).like(like),
                func.lower(Contact.company_name).like(like),
                func.lower(Contact.company_domain).like(like),
            )
        )
    if keyword:
        like = f"%{keyword.lower()}%"
        query = query.filter(
            or_(
                func.lower(ContactContext.auto_context_short).like(like),
                func.lower(ContactContext.auto_context_detailed).like(like),
            )
        )

    sort_column = {
        "list_number": Contact.list_number,
        "last_contacted_at": Contact.last_contacted_at,
        "first_contacted_at": Contact.first_contacted_at,
        "email_count": Contact.email_count,
        "fundraising_relevance_score": Contact.fundraising_relevance_score,
        "full_name": Contact.full_name,
        "company_name": Contact.company_name,
    }.get(sort, Contact.last_contacted_at)

    if order == "asc":
        query = query.order_by(sort_column.asc().nullslast())
    else:
        query = query.order_by(sort_column.desc().nullslast())

    total = query.count()
    contacts = query.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "items": [_hydrate_list_item(c) for c in contacts],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _remote_sent_total(db: Session, mailbox_id: str | None) -> int | None:
    """Sent-folder count straight from the provider, for the "have we imported everything?" card.

    Returns None when the count cannot be obtained cheaply: Gmail would need a full IMAP login on
    every stats call, and a delegated mailbox needs a stored sign-in. None renders as "unknown"
    rather than as zero.
    """
    try:
        mailbox = get_mailbox(mailbox_id) if mailbox_id else default_mailbox()
    except MailboxConfigError:
        return None
    if mailbox is None:
        return None

    if mailbox.provider == PROVIDER_GRAPH_APP:
        try:
            token = acquire_app_token(mailbox.app_credentials)
            with httpx.Client(timeout=30.0, trust_env=False) as client:
                response = client.get(
                    f"{GRAPH_BASE}/users/{mailbox.from_email}/mailFolders/sentitems"
                    "?$select=totalItemCount",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if response.status_code == 200:
                return response.json().get("totalItemCount")
        except (GraphAppAuthError, httpx.HTTPError):
            return None
        return None

    if mailbox.provider == PROVIDER_GRAPH:
        try:
            return GraphClient(db).fetch_sent_items_folder().get("totalItemCount")
        except (GraphAuthError, httpx.HTTPError):
            return None

    return None


@router.get("/stats", response_model=StatsOut)
def contact_stats(mailbox_id: str | None = None, db: Session = Depends(get_db)):
    external_filter = (Contact.is_internal.is_(False), Contact.is_excluded.is_(False))

    total_contacts = db.query(func.count(Contact.id)).scalar() or 0
    external_contacts = db.query(func.count(Contact.id)).filter(*external_filter).scalar() or 0
    high_relevance = (
        db.query(func.count(Contact.id)).filter(Contact.fundraising_relevance_tier == "high").scalar() or 0
    )
    total_messages = db.query(func.count(EmailMessage.id)).scalar() or 0
    synced_query = db.query(func.count(EmailMessage.id)).filter(
        ~EmailMessage.graph_message_id.like("test-%")
    )
    if mailbox_id:
        synced_query = synced_query.filter(EmailMessage.mailbox_id == mailbox_id)
    synced_messages = synced_query.scalar() or 0
    review_pending = (
        db.query(func.count(Contact.id)).filter(*external_filter, Contact.review_status == "pending").scalar() or 0
    )
    review_approved = (
        db.query(func.count(Contact.id)).filter(*external_filter, Contact.review_status == "approved").scalar() or 0
    )
    review_denied = (
        db.query(func.count(Contact.id)).filter(*external_filter, Contact.review_status == "denied").scalar() or 0
    )

    graph_sent_total = _remote_sent_total(db, mailbox_id)
    sync_complete = synced_messages >= graph_sent_total if graph_sent_total is not None else None

    last_sync_query = db.query(SyncRun).filter(SyncRun.status == "completed")
    if mailbox_id:
        last_sync_query = last_sync_query.filter(SyncRun.mailbox_id == mailbox_id)
    last_sync = last_sync_query.order_by(SyncRun.completed_at.desc()).first()
    return StatsOut(
        total_contacts=total_contacts,
        external_contacts=external_contacts,
        high_relevance_contacts=high_relevance,
        total_messages=total_messages,
        synced_messages=synced_messages,
        graph_sent_total=graph_sent_total,
        sync_complete=sync_complete,
        review_pending=review_pending,
        review_approved=review_approved,
        review_denied=review_denied,
        last_sync_at=last_sync.completed_at if last_sync else None,
    )


@router.get("/{contact_id}", response_model=ContactDetail)
def get_contact(contact_id: str, db: Session = Depends(get_db)):
    contact = (
        db.query(Contact)
        .options(joinedload(Contact.context), joinedload(Contact.email_links).joinedload(ContactEmailLink.message))
        .filter(Contact.id == contact_id)
        .one_or_none()
    )
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    base = _hydrate_list_item(contact)
    context = contact.context
    return ContactDetail(
        **base.model_dump(),
        score_breakdown=contact.score_breakdown,
        auto_context_detailed=context.auto_context_detailed if context else None,
        last_meaningful_email_preview=context.last_meaningful_email_preview if context else None,
        meaningful_previews=context.meaningful_previews if context else None,
        ai_summary=context.ai_summary if context else None,
        ai_relationship_context=context.ai_relationship_context if context else None,
        ai_follow_up_draft=context.ai_follow_up_draft if context else None,
        ai_contact_classification=context.ai_contact_classification if context else None,
        ai_summary_generated_at=context.ai_summary_generated_at if context else None,
    )


@router.patch("/{contact_id}", response_model=ContactDetail)
def update_contact(contact_id: str, payload: ContactUpdate, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.id == contact_id).one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    updates = payload.model_dump(exclude_unset=True)
    if "review_status" in updates and updates["review_status"] not in REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail="review_status must be pending, approved, or denied")
    for field, value in updates.items():
        setattr(contact, field, value)
    contact.updated_at = datetime.utcnow()
    db.commit()
    return get_contact(contact_id, db)


@router.get("/{contact_id}/messages")
def contact_messages(contact_id: str, db: Session = Depends(get_db)):
    messages = (
        db.query(EmailMessage)
        .join(ContactEmailLink, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id == contact_id)
        .order_by(EmailMessage.sent_datetime.desc())
        .all()
    )
    return [
        {
            "id": m.id,
            "subject": m.subject,
            "sent_datetime": m.sent_datetime,
            "body_preview": m.body_preview,
            "outlook_weblink": m.outlook_weblink,
            "has_attachments": m.has_attachments,
            "conversation_id": m.conversation_id,
        }
        for m in messages
    ]
