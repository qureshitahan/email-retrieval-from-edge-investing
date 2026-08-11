"use client";

import { useState } from "react";

import { ActivityNugget, EmailDraft, MailboxStatus, Personalization } from "@/lib/api";

/**
 * What the draft was personalised on.
 *
 * Every line here was verified against the recipient's own mail before the draft was written,
 * so this doubles as the check on the opening sentence: if the email congratulates someone on
 * a deal, the quote that claim came from is one click away. When nothing was verified it says
 * so plainly — an email that opens on the last message is a correct outcome, not a failure.
 */
function BriefItems({ items, open }: { items: ActivityNugget[]; open: boolean }) {
  return (
    <ul className="draft-brief-list">
      {items.map((item, i) => (
        <li key={i}>
          <span className="draft-brief-headline">{item.headline}</span>
          <span className="meta">
            {item.date || "date unknown"}
            {item.said_by === "us" ? " · you said this to them" : " · they told you"}
            {item.is_recent ? "" : " · over a year ago"}
          </span>
          {open && item.quote && (
            <blockquote className="draft-brief-quote">
              &ldquo;{item.quote}&rdquo;
              {item.source_subject && <cite> — {item.source_subject}</cite>}
            </blockquote>
          )}
        </li>
      ))}
    </ul>
  );
}

function PersonalizationPanel({ data }: { data: Personalization }) {
  const [open, setOpen] = useState(false);
  const activity = data.activity || [];
  const aboutThem = data.about_them || [];
  const focus = data.focus || [];
  const total = activity.length + aboutThem.length;
  const readNote = data.studied_messages
    ? `read ${data.studied_messages} message${data.studied_messages === 1 ? "" : "s"}` +
      (data.full_bodies_read ? `, ${data.full_bodies_read} in full` : "")
    : "";

  // Only when the mail genuinely establishes nothing about this person. Made rare on purpose:
  // an offer they made or a problem they described is just as personal as an achievement.
  if (total === 0 && focus.length === 0) {
    return (
      <div className="draft-brief empty">
        <span className="draft-brief-title">Nothing quotable about them</span>
        <span className="meta">
          {data.reason || "nothing in the mail could be quoted"} — opened on your last exchange
          instead{readNote ? ` (${readNote})` : ""}.
        </span>
      </div>
    );
  }

  return (
    <div className="draft-brief">
      <button className="draft-brief-head" onClick={() => setOpen(!open)} type="button">
        <span className="draft-brief-title">
          What we know they&apos;re doing
          {total > 0 && <span className="draft-brief-count">{total}</span>}
        </span>
        <span className="meta">{open ? "hide evidence" : "show evidence"}</span>
      </button>

      <p className="draft-brief-explainer">
        Facts about this person taken from your own email history with them, and used to write
        the email below. <strong>Show evidence</strong> reveals the exact sentence each one came
        from, so you can check any claim before you send.
        {readNote ? ` We ${readNote} for this.` : ""}
      </p>

      {activity.length > 0 && <BriefItems items={activity} open={open} />}

      {aboutThem.length > 0 && (
        <>
          {activity.length > 0 && (
            <span className="draft-brief-sub">Also on record</span>
          )}
          <BriefItems items={aboutThem} open={open} />
        </>
      )}

      {open && focus.length > 0 && (
        <div className="draft-brief-focus">
          <span className="meta">Working on: {focus.join(" · ")}</span>
        </div>
      )}
      {open && data.note && <div className="meta draft-brief-note">{data.note}</div>}
      {open && readNote && <div className="meta draft-brief-note">Studied: {readNote}.</div>}
    </div>
  );
}

/**
 * One draft, reviewed on its own before anything is sent.
 *
 * The FROM selector is pre-set to the mailbox that already corresponds with the recipient, so
 * a batch of drafts is correctly addressed without touching each one — but it stays editable,
 * because the routing is a sensible default rather than a rule the sender cannot override.
 */
export function DraftCard({
  draft,
  index,
  total,
  mailboxes,
  selected,
  onToggle,
  onChangeMailbox,
  onEdit,
  onSend,
  onRegenerate,
  onDelete,
  busy,
  signature,
}: {
  draft: EmailDraft;
  index: number;
  total: number;
  mailboxes: MailboxStatus[];
  selected: boolean;
  onToggle: () => void;
  onChangeMailbox: (mailboxId: string) => void;
  onEdit: (patch: { subject?: string; body?: string }) => void;
  onSend: () => void;
  onRegenerate: () => void;
  onDelete: () => void;
  busy: boolean;
  /** The block appended to this draft, so the line count measures only the written message. */
  signature?: string;
}) {
  const sendable = mailboxes.filter((m) => m.can_send);
  const from = draft.sending_mailbox_id || "";
  const fromMailbox = mailboxes.find((m) => m.id === from) || null;
  // Only the written message counts. The signature is appended afterwards and is four or five
  // lines on its own, which made every correctly-sized email report "8 lines (aim for 4–5)".
  const written = signature ? (draft.body || "").replace(signature, "") : draft.body || "";
  const bodyLines = written.split("\n").filter((l) => l.trim()).length;
  const isSent = draft.status === "sent";
  // A draft that did not generate correctly is kept visible so it can be regenerated, but it
  // must never look ready: no ticking it, no sending it.
  const hasFailed = draft.status === "failed";
  const locked = isSent || hasFailed;

  return (
    <div className={`draft-card${selected ? " selected" : ""}${isSent ? " sent" : ""}${hasFailed ? " failed" : ""}`}>
      <div className="draft-card-head">
        <label className="draft-card-who">
          <input type="checkbox" checked={selected && !hasFailed} onChange={onToggle} disabled={locked} />
          <span>
            <strong>{draft.contact_name || draft.contact_email}</strong>
            <span className={`draft-status ${draft.status}`}> {draft.status}</span>
            <span className="draft-card-sub">{draft.contact_email}</span>
          </span>
        </label>
        <span className="draft-card-step">
          {index + 1} of {total}
        </span>
      </div>

      <div className="draft-field">
        <label>FROM</label>
        {sendable.length === 0 ? (
          <span className="meta">No mailbox can send — check OUTREACH_MAILBOXES.</span>
        ) : (
          <select
            value={from}
            onChange={(e) => onChangeMailbox(e.target.value)}
            disabled={locked}
          >
            {!from && <option value="">Choose a mailbox…</option>}
            {sendable.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label} — {m.from_email}
              </option>
            ))}
          </select>
        )}
      </div>

      <div className="draft-field">
        <label>TO</label>
        <span className="draft-to">{draft.contact_email}</span>
      </div>

      {draft.personalization && <PersonalizationPanel data={draft.personalization} />}

      {(draft.personalization?.selection_reason || draft.personalization?.objective) && (
        <div className="draft-why">
          {draft.personalization.objective && (
            <div className="draft-why-row">
              <span className="draft-why-label">Topic</span>
              <span>{draft.personalization.objective}</span>
            </div>
          )}
          {draft.personalization.selection_reason && (
            <div className="draft-why-row">
              <span className="draft-why-label">Why them</span>
              <span>
                {typeof draft.personalization.selection_score === "number" && (
                  <strong className="draft-why-score">
                    {draft.personalization.selection_score}
                  </strong>
                )}
                {draft.personalization.selection_reason}
              </span>
            </div>
          )}
        </div>
      )}

      <div className="draft-field">
        <label>SUBJECT</label>
        <input
          value={draft.subject || ""}
          onChange={(e) => onEdit({ subject: e.target.value })}
          disabled={isSent}
        />
      </div>

      <div className="draft-field">
        <label>BODY</label>
        <textarea
          rows={8}
          value={draft.body || ""}
          onChange={(e) => onEdit({ body: e.target.value })}
          disabled={isSent}
        />
      </div>

      <div className={`draft-lines${bodyLines > 6 ? " over" : ""}`}>
        {bodyLines} lines (aim for 4–5)
      </div>

      {draft.error_message && <div className="banner error">{draft.error_message}</div>}

      <div className="draft-card-foot">
        <span className="meta">
          {isSent
            ? `Sent${fromMailbox ? ` from ${fromMailbox.from_email}` : ""}`
            : fromMailbox
              ? `Sending from ${fromMailbox.from_email}`
              : "No sending mailbox chosen"}
        </span>
        <div className="actions">
          <button className="link-btn" onClick={onRegenerate} disabled={busy || isSent}>
            Regenerate
          </button>
          <button className="link-btn danger" onClick={onDelete} disabled={busy || isSent}>
            Delete
          </button>
          <button className="primary" onClick={onSend} disabled={busy || locked || !from}>
            {isSent ? "Sent" : "Send this one"}
          </button>
        </div>
      </div>
    </div>
  );
}
