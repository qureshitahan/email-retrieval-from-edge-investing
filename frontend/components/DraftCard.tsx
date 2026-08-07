"use client";

import { useState } from "react";

import { EmailDraft, MailboxStatus, Personalization } from "@/lib/api";

/**
 * What the draft was personalised on.
 *
 * Every line here was verified against the recipient's own mail before the draft was written,
 * so this doubles as the check on the opening sentence: if the email congratulates someone on
 * a deal, the quote that claim came from is one click away. When nothing was verified it says
 * so plainly — an email that opens on the last message is a correct outcome, not a failure.
 */
function PersonalizationPanel({ data }: { data: Personalization }) {
  const [open, setOpen] = useState(false);
  const activity = data.activity || [];
  const focus = data.focus || [];

  if (activity.length === 0 && focus.length === 0) {
    return (
      <div className="draft-brief empty">
        <span className="draft-brief-title">Nothing recent found about them</span>
        <span className="meta">
          {data.reason || "no concrete activity in the mail"} — opened on your last exchange
          instead{data.studied_messages ? ` (read ${data.studied_messages} messages)` : ""}.
        </span>
      </div>
    );
  }

  return (
    <div className="draft-brief">
      <button className="draft-brief-head" onClick={() => setOpen(!open)} type="button">
        <span className="draft-brief-title">
          What we know they&apos;re doing
          {activity.length > 0 && <span className="draft-brief-count">{activity.length}</span>}
        </span>
        <span className="meta">{open ? "hide" : "show evidence"}</span>
      </button>

      <ul className="draft-brief-list">
        {activity.map((item, i) => (
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
                {item.source_subject && (
                  <cite> — {item.source_subject}</cite>
                )}
              </blockquote>
            )}
          </li>
        ))}
      </ul>

      {open && focus.length > 0 && (
        <div className="draft-brief-focus">
          <span className="meta">Working on: {focus.join(" · ")}</span>
        </div>
      )}
      {open && data.note && <div className="meta draft-brief-note">{data.note}</div>}
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
}) {
  const sendable = mailboxes.filter((m) => m.can_send);
  const from = draft.sending_mailbox_id || "";
  const fromMailbox = mailboxes.find((m) => m.id === from) || null;
  const bodyLines = (draft.body || "").split("\n").filter((l) => l.trim()).length;
  const isSent = draft.status === "sent";

  return (
    <div className={`draft-card${selected ? " selected" : ""}${isSent ? " sent" : ""}`}>
      <div className="draft-card-head">
        <label className="draft-card-who">
          <input type="checkbox" checked={selected} onChange={onToggle} disabled={isSent} />
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
            disabled={isSent}
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
          <button className="primary" onClick={onSend} disabled={busy || isSent || !from}>
            {isSent ? "Sent" : "Send this one"}
          </button>
        </div>
      </div>
    </div>
  );
}
