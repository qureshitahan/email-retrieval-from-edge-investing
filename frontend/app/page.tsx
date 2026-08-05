"use client";

import { useCallback, useEffect, useState } from "react";
import {
  api,
  AIResult,
  Contact,
  ContactDetail,
  formatDate,
  MailboxStatus,
  Stats,
  SyncProgress,
  SyncRun,
  tierClass,
  reviewClass,
} from "@/lib/api";
import { InfoTip, SectionHeading } from "@/components/InfoTip";
import { MailboxPicker } from "@/components/MailboxPicker";
import { Nav } from "@/components/Nav";
import { SyncProgressBars } from "@/components/SyncProgressBars";
import { RelevanceTierHelp, ScoreBreakdownHelp } from "@/lib/helpText";

export default function HomePage() {
  // Connection state comes from /mailboxes now — it covers all three transports, not just the
  // interactive Outlook sign-in that /auth/status reports on.
  const [mailboxes, setMailboxes] = useState<MailboxStatus[]>([]);
  const [activeMailbox, setActiveMailbox] = useState("");
  const [stats, setStats] = useState<Stats | null>(null);
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<ContactDetail | null>(null);
  const [messages, setMessages] = useState<
    Array<{ id: string; subject: string; sent_datetime: string; body_preview: string; outlook_weblink: string }>
  >([]);
  const [sync, setSync] = useState<SyncRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [backfilling, setBackfilling] = useState(false);
  const [progress, setProgress] = useState<SyncProgress[]>([]);
  const [aiLoading, setAiLoading] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [q, setQ] = useState("");
  const [fundraisingTier, setFundraisingTier] = useState("");
  const [emailCountMin, setEmailCountMin] = useState("");
  const [reviewFilter, setReviewFilter] = useState("");
  // Selecting a mailbox scopes the list to that mailbox's contacts. This is the escape hatch
  // for seeing everyone at once, including messages imported before mailbox attribution existed.
  const [showAllMailboxes, setShowAllMailboxes] = useState(false);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [statsData, contactData, syncData, mailboxData] = await Promise.all([
        api.stats(showAllMailboxes ? undefined : activeMailbox || undefined),
        api.contacts({
          page,
          page_size: 50,
          q,
          fundraising_tier: fundraisingTier,
          email_count_min: emailCountMin || "",
          review_status: reviewFilter,
          exclude_internal: true,
          exclude_noise: true,
          mailbox_id: showAllMailboxes ? "" : activeMailbox,
          sort: "list_number",
          order: "asc",
        }),
        api.syncStatus(activeMailbox || undefined),
        // Mailbox readiness must not be able to break the contacts list.
        api.mailboxStatuses().catch(() => ({ items: [] as MailboxStatus[], config_error: null })),
      ]);
      setStats(statsData);
      setContacts(contactData.items);
      setTotal(contactData.total);
      setSync(syncData);
      setMailboxes(mailboxData.items);
      setActiveMailbox((prev) => {
        if (prev && mailboxData.items.some((m) => m.id === prev)) return prev;
        // Prefer one that needs no user action; otherwise just show the first.
        return (
          mailboxData.items.find((m) => m.connected)?.id || mailboxData.items[0]?.id || ""
        );
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load data");
    } finally {
      setLoading(false);
    }
  }, [page, q, fundraisingTier, emailCountMin, reviewFilter, showAllMailboxes, activeMailbox]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // Progress polls continuously — not only while this tab started a sync — so a run kicked
  // off elsewhere (or still going after a reload) still shows its bars.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    async function poll() {
      try {
        const data = await api.syncProgress();
        if (cancelled) return;
        setProgress(data.items);
        // Refresh the contact list while a sync runs so rows appear as they are imported,
        // rather than only once the whole mailbox has finished.
        if (data.any_running) {
          setSyncing(true);
          loadAll();
        } else if (syncing) {
          setSyncing(false);
          loadAll();
        }
        timer = setTimeout(poll, data.any_running ? 4000 : 20000);
      } catch {
        // A failed poll must not kill the loop; back off and try again.
        if (!cancelled) timer = setTimeout(poll, 15000);
      }
    }

    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!sync || sync.status !== "running") return;
    // Poll the run we actually started, not whichever run happens to be newest.
    const polledMailbox = sync.mailbox_id || undefined;
    const timer = setInterval(async () => {
      const status = await api.syncStatus(polledMailbox);
      setSync(status);
      if (status?.status !== "running") {
        setSyncing(false);
        loadAll();
      }
    }, 4000);
    return () => clearInterval(timer);
  }, [sync, loadAll]);

  async function handleSync() {
    setSyncing(true);
    setError(null);
    try {
      const run = await api.startSync(activeMailbox || undefined);
      setSync(run);
    } catch (err) {
      setSyncing(false);
      setError(err instanceof Error ? err.message : "Sync failed to start");
    }
  }

  async function handleInboxSync() {
    setSyncing(true);
    setError(null);
    try {
      const run = await api.startInboxSync(activeMailbox || undefined);
      setSync(run);
    } catch (err) {
      setSyncing(false);
      setError(err instanceof Error ? err.message : "Inbox sync failed to start");
    }
  }

  async function handleBackfill() {
    if (!selectedMailbox) return;
    setBackfilling(true);
    setError(null);
    try {
      const result = await api.backfillMailbox(selectedMailbox.id);
      await loadAll();
      setError(
        `Attributed ${result.messages_updated.toLocaleString()} messages to ${result.from_email}.` +
          (result.messages_still_unattributed
            ? ` ${result.messages_still_unattributed.toLocaleString()} still unattributed.`
            : "")
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Backfill failed");
    } finally {
      setBackfilling(false);
    }
  }

  async function setReviewStatus(contactId: string, review_status: string) {
    try {
      const updated = await api.updateContact(contactId, { review_status });
      setContacts((prev) =>
        prev.map((c) => (c.id === contactId ? { ...c, review_status: updated.review_status } : c))
      );
      if (selected?.id === contactId) {
        setSelected(updated);
      }
      const statsData = await api.stats(
        showAllMailboxes ? undefined : activeMailbox || undefined
      );
      setStats(statsData);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update review status");
    }
  }

  async function openContact(contact: Contact) {
    setDetailLoading(true);
    setSelected({ ...contact } as ContactDetail);
    setMessages([]);
    try {
      const [detail, timeline] = await Promise.all([
        api.contact(contact.id),
        api.contactMessages(contact.id),
      ]);
      setSelected(detail);
      setMessages(timeline);
    } catch (err) {
      setSelected(null);
      setError(err instanceof Error ? err.message : "Failed to load contact");
    } finally {
      setDetailLoading(false);
    }
  }

  function rowClass(contact: Contact) {
    const classes: string[] = [];
    if (selected?.id === contact.id) classes.push("row-selected");
    else if (contact.review_status === "approved") classes.push("row-approved");
    else if (contact.review_status === "denied") classes.push("row-denied");
    return classes.join(" ");
  }

  async function runAiAction(action: "summary" | "threads", force = false) {
    if (!selected) return;
    setAiLoading(action);
    setError(null);
    try {
      let result: AIResult;
      switch (action) {
        case "summary":
          result = await api.aiSummary(selected.id, force);
          setSelected((prev) =>
            prev ? { ...prev, ai_summary: result.summary || prev.ai_summary } : prev
          );
          break;
        case "threads":
          result = await api.aiSummarizeThreads(selected.id, force);
          setSelected((prev) =>
            prev ? { ...prev, ai_summary: result.summary || prev.ai_summary } : prev
          );
          break;
      }
      const refreshed = await api.contact(selected.id);
      setSelected(refreshed);
    } catch (err) {
      setError(err instanceof Error ? err.message : "AI request failed");
    } finally {
      setAiLoading(null);
    }
  }

  const selectedMailbox = mailboxes.find((m) => m.id === activeMailbox) || null;
  const readableMailboxes = mailboxes.filter((m) => m.can_read);
  const signInMailbox = mailboxes.find((m) => m.needs_signin) || null;

  return (
    <main className="page">
      <div className="header">
        <div>
          <Nav />
          <h1 style={{ marginTop: 12 }}>Relationship Intelligence CRM</h1>
          <p>
            Outlook Sent Items → contacts, context, and fundraising relevance
            {selectedMailbox ? ` · ${selectedMailbox.from_email}` : ""}
          </p>
        </div>
        <div className="actions">
          <MailboxPicker
            mailboxes={mailboxes}
            value={activeMailbox}
            onChange={setActiveMailbox}
            capability="read"
            compact
          />
          <button
            className="primary"
            onClick={handleSync}
            disabled={syncing || sync?.status === "running" || !selectedMailbox?.can_read}
            title={selectedMailbox?.detail}
          >
            {sync?.status === "running" ? "Syncing…" : "Sync Sent Items"}
          </button>
          <button
            onClick={handleInboxSync}
            disabled={syncing || sync?.status === "running" || !selectedMailbox?.can_read}
            title={selectedMailbox?.detail}
          >
            Sync Inbox
          </button>
          {/* Shown whenever ANY mailbox needs the sign-in, not just the selected one - otherwise
              it hides itself exactly when the user is looking for it. */}
          {signInMailbox && (
            <a className="button primary" href={api.loginUrl()} title={signInMailbox.detail}>
              Sign in to {signInMailbox.from_email}
            </a>
          )}
          <a className="button" href={api.exportXlsxUrl()}>
            Export Excel
          </a>
          <a className="button" href={api.exportCsvUrl()}>
            Export CSV
          </a>
        </div>
      </div>

      {error && <div className="banner error">{error}</div>}

      <SyncProgressBars items={progress} activeMailboxId={activeMailbox} />

      {stats && stats.unattributed_messages > 0 && selectedMailbox && (
        <div className="banner info">
          <strong>{stats.unattributed_messages.toLocaleString()} messages</strong> were imported
          before per-mailbox tracking existed, so they belong to no mailbox and are hidden unless{" "}
          <strong>Show all mailboxes</strong> is ticked. They came from the one Outlook account that
          could be connected at the time.
          <div className="actions" style={{ marginTop: 8 }}>
            <button onClick={handleBackfill} disabled={backfilling}>
              {backfilling
                ? "Attributing…"
                : `Attribute them to ${selectedMailbox.from_email}`}
            </button>
          </div>
        </div>
      )}

      {mailboxes.length > 0 && readableMailboxes.length < mailboxes.length && (
        <div className="banner info">
          <strong>
            {readableMailboxes.length} of {mailboxes.length} mailboxes can be synced.
          </strong>
          <ul className="banner-list">
            {mailboxes
              .filter((m) => !m.can_read)
              .map((m) => (
                <li key={m.id}>
                  <strong>{m.from_email}</strong> — {m.detail}
                </li>
              ))}
          </ul>
        </div>
      )}

      {sync?.status === "running" && (
        <div className="banner info">
          Sync in progress: {sync.messages_fetched.toLocaleString()} messages fetched,{" "}
          {sync.messages_new.toLocaleString()} new imported.
        </div>
      )}
      {sync?.status === "completed" && sync.completed_at && (
        <div className="banner success">
          Last sync completed: {sync.messages_fetched.toLocaleString()} messages,{" "}
          {sync.messages_new.toLocaleString()} new.
        </div>
      )}
      {sync?.status === "failed" && (
        <div className="banner error">Sync failed: {sync.error_message}</div>
      )}

      {stats && (
        <div className="stats">
          <div className="stat-card">
            <div className="label">Outlook Sent Items</div>
            <div className="value" style={{ fontSize: stats.graph_sent_total ? "1.4rem" : "1rem" }}>
              {stats.graph_sent_total != null
                ? stats.sync_complete
                  ? `${stats.synced_messages.toLocaleString()} ✓`
                  : `${stats.synced_messages.toLocaleString()} / ${stats.graph_sent_total.toLocaleString()}`
                : "Connect to verify"}
            </div>
            {stats.sync_complete === true && (
              <div className="stat-note">All sent emails imported</div>
            )}
            {stats.sync_complete === false && (
              <div className="stat-note warn">Run sync again to fetch remaining</div>
            )}
          </div>
          <div className="stat-card">
            <div className="label">To review</div>
            <div className="value">{stats.review_pending.toLocaleString()}</div>
          </div>
          <div className="stat-card">
            <div className="label">Approved to email</div>
            <div className="value">{stats.review_approved.toLocaleString()}</div>
          </div>
          <div className="stat-card">
            <div className="label">Denied</div>
            <div className="value">{stats.review_denied.toLocaleString()}</div>
          </div>
          <div className="stat-card">
            <div className="label">External contacts</div>
            <div className="value">{stats.external_contacts.toLocaleString()}</div>
          </div>
          <div className="stat-card">
            <div className="label">Last sync</div>
            <div className="value" style={{ fontSize: "1rem" }}>
              {formatDate(stats.last_sync_at)}
            </div>
          </div>
        </div>
      )}

      <div className={`layout-with-drawer${selected ? " has-drawer" : ""}`}>
        <div>
          <div className="panel">
            <div className="filters">
              <input
                placeholder="Search name, email, company, domain…"
                value={q}
                onChange={(e) => {
                  setPage(1);
                  setQ(e.target.value);
                }}
              />
              <select
                value={fundraisingTier}
                onChange={(e) => {
                  setPage(1);
                  setFundraisingTier(e.target.value);
                }}
              >
                <option value="">All relevance</option>
                <option value="high">High</option>
                <option value="medium">Medium</option>
                <option value="low">Low</option>
              </select>
              <input
                placeholder="Min emails"
                value={emailCountMin}
                onChange={(e) => {
                  setPage(1);
                  setEmailCountMin(e.target.value);
                }}
              />
              <select
                value={reviewFilter}
                onChange={(e) => {
                  setPage(1);
                  setReviewFilter(e.target.value);
                }}
              >
                <option value="">All review status</option>
                <option value="pending">To review</option>
                <option value="approved">Approved</option>
                <option value="denied">Denied</option>
              </select>
              {mailboxes.length > 1 && (
                <label
                  className="inline-check"
                  title="By default the list shows only the selected mailbox's contacts"
                >
                  <input
                    type="checkbox"
                    checked={showAllMailboxes}
                    onChange={(e) => {
                      setPage(1);
                      setShowAllMailboxes(e.target.checked);
                    }}
                  />
                  Show all mailboxes
                </label>
              )}
            </div>
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Name</th>
                  <th>Email</th>
                  <th>Company</th>
                  <th>Last</th>
                  <th>Emails</th>
                  <th>Relevance <InfoTip label="How relevance tiers work"><RelevanceTierHelp /></InfoTip></th>
                  <th>Review</th>
                  <th>Topics</th>
                  <th>Last subject</th>
                  <th>Outlook</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr>
                    <td colSpan={11}>Loading…</td>
                  </tr>
                ) : contacts.length === 0 ? (
                  <tr>
                    <td colSpan={11}>
                      {!showAllMailboxes && selectedMailbox ? (
                        <>
                          No contacts from <strong>{selectedMailbox.from_email}</strong> yet.{" "}
                          {selectedMailbox.can_read
                            ? "Click Sync Sent Items to import them."
                            : selectedMailbox.detail}{" "}
                          Tick <strong>Show all mailboxes</strong> to see contacts from the others.
                        </>
                      ) : (
                        "No contacts yet. Pick a mailbox and run a sync to import Sent Items."
                      )}
                    </td>
                  </tr>
                ) : (
                  contacts.map((contact) => (
                    <tr
                      key={contact.id}
                      onClick={() => openContact(contact)}
                      style={{ cursor: "pointer" }}
                      className={rowClass(contact)}
                      aria-selected={selected?.id === contact.id}
                    >
                      <td className="serial">{contact.list_number ?? "—"}</td>
                      <td>{contact.full_name}</td>
                      <td>{contact.primary_email}</td>
                      <td>{contact.company_name}</td>
                      <td>{formatDate(contact.last_contacted_at)}</td>
                      <td>
                        {contact.email_count} / {contact.thread_count}
                      </td>
                      <td>
                        <span className={tierClass(contact.fundraising_relevance_tier)}>
                          {contact.fundraising_relevance_tier || "low"} ({contact.fundraising_relevance_score})
                        </span>
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <div className="review-actions">
                          <span className={reviewClass(contact.review_status)}>
                            {contact.review_status}
                          </span>
                          <button
                            className="review-btn approve"
                            title="Approve — email later"
                            onClick={() => setReviewStatus(contact.id, "approved")}
                          >
                            ✓
                          </button>
                          <button
                            className="review-btn deny"
                            title="Deny — not interested"
                            onClick={() => setReviewStatus(contact.id, "denied")}
                          >
                            ✕
                          </button>
                        </div>
                      </td>
                      <td>
                        <div className="chips">
                          {(contact.detected_topics || []).slice(0, 3).map((topic) => (
                            <span className="chip" key={topic}>
                              {topic}
                            </span>
                          ))}
                        </div>
                      </td>
                      <td className="preview">{contact.last_subject}</td>
                      <td>
                        {contact.latest_message_id ? (
                          <a
                            href={api.openOutlookUrl(contact.latest_message_id)}
                            target="_blank"
                            rel="noreferrer"
                            onClick={(e) => e.stopPropagation()}
                          >
                            Open
                          </a>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          <div className="pagination">
            <span>
              Showing {contacts.length} of {total.toLocaleString()} contacts
            </span>
            <div style={{ display: "flex", gap: 8 }}>
              <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                Previous
              </button>
              <button
                disabled={page * 50 >= total}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </button>
            </div>
          </div>
        </div>

        {selected && (
          <aside className="drawer">
            <div className="drawer-header">
              <button className="drawer-close" onClick={() => setSelected(null)} aria-label="Close">
                ✕
              </button>
              <div className="drawer-title">
                <span className="drawer-number">#{selected.list_number}</span>
                <h2>{selected.full_name || selected.primary_email}</h2>
              </div>
              <div className="drawer-meta">{selected.company_name || "—"}</div>
              <div className="drawer-meta">{selected.primary_email}</div>
              <div className="drawer-badges">
                <span className={tierClass(selected.fundraising_relevance_tier)}>
                  {selected.fundraising_relevance_tier || "low"} ({selected.fundraising_relevance_score})
                </span>
                <InfoTip label="How relevance tiers work">
                  <RelevanceTierHelp />
                </InfoTip>
                {selected.contact_type && <span className="chip">{selected.contact_type}</span>}
                <span className={reviewClass(selected.review_status)}>{selected.review_status}</span>
              </div>
              <div className="review-actions drawer-review">
                <button className="review-btn approve" onClick={() => setReviewStatus(selected.id, "approved")}>
                  Approve to email
                </button>
                <button className="review-btn deny" onClick={() => setReviewStatus(selected.id, "denied")}>
                  Deny
                </button>
                {selected.review_status !== "pending" && (
                  <button className="review-btn reset" onClick={() => setReviewStatus(selected.id, "pending")}>
                    Reset
                  </button>
                )}
              </div>
            </div>

            {detailLoading ? (
              <div className="drawer-loading">Loading contact details…</div>
            ) : (
              <>
                <div className="drawer-stats">
                  <div className="drawer-stat">
                    <span className="label">Emails</span>
                    <span className="value">{selected.email_count}</span>
                  </div>
                  <div className="drawer-stat">
                    <span className="label">Threads</span>
                    <span className="value">{selected.thread_count}</span>
                  </div>
                  <div className="drawer-stat">
                    <span className="label">First contact</span>
                    <span className="value">{formatDate(selected.first_contacted_at)}</span>
                  </div>
                  <div className="drawer-stat">
                    <span className="label">Last contact</span>
                    <span className="value">{formatDate(selected.last_contacted_at)}</span>
                  </div>
                </div>

                {(selected.detected_topics || []).length > 0 && (
                  <section className="drawer-section">
                    <h3>Topics</h3>
                    <div className="chips">
                      {(selected.detected_topics || []).map((topic) => (
                        <span className="chip" key={topic}>
                          {topic}
                        </span>
                      ))}
                    </div>
                  </section>
                )}

                {selected.last_subject && (
                  <section className="drawer-section">
                    <h3>Latest subject</h3>
                    <p className="drawer-subject">{selected.last_subject}</p>
                    {selected.latest_message_id && (
                      <a href={api.openOutlookUrl(selected.latest_message_id)} target="_blank" rel="noreferrer">
                        Open in Outlook →
                      </a>
                    )}
                  </section>
                )}

                <section className="drawer-section">
                  <h3>Last correspondence</h3>
                  <p className="drawer-body preview-block">
                    {selected.last_meaningful_email_preview || selected.last_preview || "No preview available."}
                  </p>
                </section>

                {selected.score_breakdown && (
                  <section className="drawer-section">
                    <SectionHeading
                      title="Score breakdown"
                      tip={
                        <InfoTip label="How scoring works">
                          <ScoreBreakdownHelp />
                        </InfoTip>
                      }
                    />
                    <div className="score-grid">
                      {Object.entries(selected.score_breakdown).map(([key, value]) => (
                        <div className="score-item" key={key}>
                          <span className="score-key">{key.replace(/_/g, " ")}</span>
                          <span className={value > 0 ? "score-pos" : value < 0 ? "score-neg" : ""}>
                            {value > 0 ? "+" : ""}
                            {value}
                          </span>
                        </div>
                      ))}
                    </div>
                  </section>
                )}

                <section className="drawer-section">
                  <h3>AI actions</h3>
                  <div className="ai-actions-grid">
                    <button disabled={!!aiLoading} onClick={() => runAiAction("summary")}>
                      {aiLoading === "summary" ? "Generating…" : selected.ai_summary ? "Regenerate Summary" : "Generate Summary"}
                    </button>
                    <button disabled={!!aiLoading} onClick={() => runAiAction("threads")}>
                      {aiLoading === "threads" ? "Summarizing…" : "Deep Thread Summary"}
                    </button>
                  </div>
                  {selected.ai_summary && (
                    <div className="ai-panel">
                      <h4>AI Summary</h4>
                      <p style={{ whiteSpace: "pre-wrap" }}>{selected.ai_summary}</p>
                      {selected.ai_summary_generated_at && (
                        <small className="meta">Generated {formatDate(selected.ai_summary_generated_at)}</small>
                      )}
                    </div>
                  )}
                </section>

                <section className="drawer-section">
                  <h3>Email timeline ({messages.length})</h3>
                  {messages.length === 0 ? (
                    <p className="drawer-empty">No emails found for this contact.</p>
                  ) : (
                    messages.map((message) => (
                      <div className="message-item" key={message.id}>
                        <div className="date">{formatDate(message.sent_datetime)}</div>
                        <strong>{message.subject}</strong>
                        <p className="preview-block">{message.body_preview}</p>
                        <a href={api.openOutlookUrl(message.id)} target="_blank" rel="noreferrer">
                          Open in Outlook →
                        </a>
                      </div>
                    ))
                  )}
                </section>
              </>
            )}
          </aside>
        )}
      </div>
    </main>
  );
}
