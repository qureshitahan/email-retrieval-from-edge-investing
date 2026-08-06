"use client";

import { EmailDraft, MailboxStatus } from "@/lib/api";

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
