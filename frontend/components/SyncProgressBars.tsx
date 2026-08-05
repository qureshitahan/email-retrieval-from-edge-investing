"use client";

import { SyncProgress } from "@/lib/api";

/**
 * Per-mailbox sync progress.
 *
 * Reports what is already stored, not just what the current run fetched, so a mailbox shows
 * its true position whether or not a sync is active — and the contacts counted here are
 * already queryable, which is the point: a sync in flight has still delivered what it read.
 */
export function SyncProgressBars({
  items,
  activeMailboxId,
}: {
  items: SyncProgress[];
  activeMailboxId?: string;
}) {
  if (items.length === 0) return null;

  return (
    <div className="sync-progress">
      {items.map((p) => {
        const pending =
          p.remote_total != null ? Math.max(0, p.remote_total - p.synced_messages) : null;
        // Gmail reports no folder total, so show an indeterminate bar while it works
        // rather than a percentage that would be invented.
        const indeterminate = p.percent === null && p.is_running;
        const width = p.percent ?? (p.is_running ? 100 : 0);

        return (
          <div
            key={p.mailbox_id}
            className={`sync-row${p.mailbox_id === activeMailboxId ? " active" : ""}`}
          >
            <div className="sync-row-head">
              <strong>{p.from_email}</strong>
              <span className={`sync-state ${p.state}`}>
                {p.is_running
                  ? `Syncing ${p.sync_type === "inbox" ? "inbox" : "sent items"}…`
                  : p.state === "failed"
                    ? "Failed"
                    : p.state === "completed"
                      ? "Synced"
                      : "Not synced yet"}
              </span>
            </div>

            <div className="sync-bar">
              <div
                className={`sync-bar-fill${indeterminate ? " indeterminate" : ""}${
                  p.state === "failed" ? " failed" : ""
                }`}
                style={{ width: `${width}%` }}
              />
            </div>

            <div className="sync-row-meta">
              <span>
                <strong>{p.synced_messages.toLocaleString()}</strong> emails synced
                {p.remote_total != null && ` of ${p.remote_total.toLocaleString()}`}
                {p.percent !== null && ` · ${p.percent}%`}
              </span>
              <span>
                {pending !== null && pending > 0 && `${pending.toLocaleString()} pending · `}
                <strong>{p.contacts.toLocaleString()}</strong> contacts found
              </span>
            </div>

            {p.state === "failed" && p.error_message && (
              <div className="sync-row-error">{p.error_message}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}
