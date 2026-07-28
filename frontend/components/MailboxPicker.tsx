"use client";

import { MailboxStatus } from "@/lib/api";

/**
 * Mailbox dropdown used in place of a "Connect" button.
 *
 * Every configured mailbox is listed, always. A mailbox that needs no user action is selectable
 * outright; one that is blocked stays visible (so the set of three never appears to shrink) but is
 * disabled and explains itself underneath, rather than replacing the whole control with a sign-in
 * prompt.
 */
export function MailboxPicker({
  mailboxes,
  value,
  onChange,
  label = "Mailbox",
  capability = "send",
  compact = false,
}: {
  mailboxes: MailboxStatus[];
  value: string;
  onChange: (id: string) => void;
  label?: string;
  /** Which capability makes a mailbox usable here. */
  capability?: "send" | "read";
  compact?: boolean;
}) {
  const usable = (m: MailboxStatus) => (capability === "read" ? m.can_read : m.can_send);
  const selected = mailboxes.find((m) => m.id === value) || null;

  if (mailboxes.length === 0) {
    return (
      <div className="mailbox-picker">
        <small className="meta">
          No mailboxes configured. Set <code>OUTREACH_MAILBOXES</code> in the backend{" "}
          <code>.env</code>.
        </small>
      </div>
    );
  }

  return (
    <div className={`mailbox-picker${compact ? " compact" : ""}`}>
      <label>
        {!compact && <span className="picker-label">{label}</span>}
        <select value={value} onChange={(e) => onChange(e.target.value)}>
          {mailboxes.map((m) => (
            <option key={m.id} value={m.id} disabled={!usable(m)}>
              {m.from_email}
              {usable(m) ? "" : ` — ${statusLabel(m, capability)}`}
            </option>
          ))}
        </select>
      </label>
      {selected && (
        <small className={`meta${usable(selected) ? "" : " warn"}`}>
          {usable(selected) ? selected.detail : statusLabel(selected, capability)}
        </small>
      )}
    </div>
  );
}

function statusLabel(m: MailboxStatus, capability: "send" | "read") {
  if (capability === "read" && m.can_send && !m.can_read) {
    return m.provider === "gmail" ? "sending only (Gmail)" : "no Mail.Read permission";
  }
  switch (m.status) {
    case "needs_signin":
      return "needs Outlook sign-in";
    case "needs_consent":
      return "awaiting Azure admin consent";
    case "not_configured":
      return "not configured";
    case "error":
      return "unreachable";
    default:
      return "unavailable";
  }
}
