"use client";

import { useCallback, useEffect, useState } from "react";
import { MailboxPicker } from "@/components/MailboxPicker";
import { Nav } from "@/components/Nav";
import {
  api,
  Contact,
  EmailDraft,
  formatDate,
  MailboxStatus,
  OutreachPrompt,
  RankedContact,
  SyncRun,
} from "@/lib/api";

export default function OutreachPage() {
  const [auth, setAuth] = useState<{
    connected: boolean;
    user_email: string | null;
    can_send_mail?: boolean;
  } | null>(null);
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [drafts, setDrafts] = useState<EmailDraft[]>([]);
  const [mailboxes, setMailboxes] = useState<MailboxStatus[]>([]);
  const [sendFrom, setSendFrom] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [activeDraft, setActiveDraft] = useState<EmailDraft | null>(null);
  const [prompt, setPrompt] = useState<OutreachPrompt | null>(null);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [userTemplate, setUserTemplate] = useState("");
  const [customInstructions, setCustomInstructions] = useState("");
  const [objective, setObjective] = useState("");
  const [ranked, setRanked] = useState<RankedContact[] | null>(null);
  const [ranking, setRanking] = useState(false);
  const [notRepliedDays, setNotRepliedDays] = useState("");
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [sending, setSending] = useState(false);
  const [sync, setSync] = useState<SyncRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showPromptEditor, setShowPromptEditor] = useState(false);
  const [showPromptUsed, setShowPromptUsed] = useState(false);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params: Record<string, string | number | boolean> = {
        review_status: "approved",
        exclude_internal: true,
        exclude_noise: true,
        page_size: 200,
        sort: "list_number",
        order: "asc",
      };
      if (notRepliedDays) {
        params.not_replied_days = Number(notRepliedDays);
        params.awaiting_reply_only = true;
      }
      const [authStatus, contactData, draftData, promptData, mailboxData] = await Promise.all([
        api.authStatus(),
        api.contacts(params),
        api.listDrafts(),
        api.getOutreachPrompt(),
        // A bad OUTREACH_MAILBOXES value must not take the whole page down.
        api
          .mailboxStatuses()
          .catch(() => ({ items: [] as MailboxStatus[], config_error: null })),
      ]);
      setAuth(authStatus);
      setContacts(contactData.items);
      setDrafts(draftData.items.filter((d) => d.status !== "sent"));
      setPrompt(promptData);
      setSystemPrompt(promptData.system_prompt);
      setUserTemplate(promptData.user_prompt_template);
      setMailboxes(mailboxData.items);
      // Keep the user's pick across reloads; fall back to the first sendable mailbox.
      setSendFrom((prev) => {
        if (prev && mailboxData.items.some((m) => m.id === prev && m.can_send)) return prev;
        return mailboxData.items.find((m) => m.can_send)?.id || "";
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load outreach data");
    } finally {
      setLoading(false);
    }
  }, [notRepliedDays]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  useEffect(() => {
    if (!sync || sync.status !== "running") return;
    const timer = setInterval(async () => {
      const status = await api.syncStatus(sync?.mailbox_id || undefined);
      setSync(status);
      if (status?.status !== "running") loadAll();
    }, 3000);
    return () => clearInterval(timer);
  }, [sync, loadAll]);

  function toggleContact(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    if (selected.size === contacts.length) setSelected(new Set());
    else setSelected(new Set(contacts.map((c) => c.id)));
  }

  async function savePromptTemplate() {
    try {
      const saved = await api.saveOutreachPrompt({
        system_prompt: systemPrompt,
        user_prompt_template: userTemplate,
      });
      setPrompt(saved);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save prompt");
    }
  }

  async function handleGenerate() {
    const ids = selected.size > 0 ? Array.from(selected) : activeDraft ? [activeDraft.contact_id] : [];
    if (ids.length === 0) {
      setError("Select at least one approved contact");
      return;
    }
    setGenerating(true);
    setError(null);
    try {
      const result = await api.generateDrafts(
        ids,
        customInstructions || undefined,
        objective || undefined
      );
      await loadAll();
      if (result.items.length > 0) setActiveDraft(result.items[0]);
      if (result.results?.some((r) => r.status === "error")) {
        const failed = result.results.filter((r) => r.status === "error");
        setError(`Some drafts failed: ${failed.map((f) => f.error).join("; ")}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Draft generation failed");
    } finally {
      setGenerating(false);
    }
  }

  async function handleSaveDraft() {
    if (!activeDraft) return;
    try {
      const updated = await api.updateDraft(activeDraft.id, {
        subject: activeDraft.subject || "",
        body: activeDraft.body || "",
      });
      setActiveDraft(updated);
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save draft");
    }
  }

  async function handleApproveDraft() {
    if (!activeDraft) return;
    try {
      const updated = await api.approveDraft(activeDraft.id);
      setActiveDraft(updated);
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to approve draft");
    }
  }

  async function handlePrioritize() {
    if (!objective.trim()) {
      setError("Enter an objective first — that is what the ranking is judged against");
      return;
    }
    setRanking(true);
    setError(null);
    try {
      // Rank the selected contacts if any are ticked, otherwise the strongest candidates.
      const result = await api.prioritize(objective.trim(), Array.from(selected), 25);
      setRanked(result.items);
      if (result.scored === 0) {
        setError("The model returned no usable scores. Try again or narrow the objective.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Prioritization failed");
    } finally {
      setRanking(false);
    }
  }

  async function handleChooseMailbox(mailboxId: string) {
    setSendFrom(mailboxId);
    if (!activeDraft || !mailboxId) return;
    // Persist the choice on the draft so a later bulk send uses the same identity.
    try {
      const updated = await api.setDraftMailbox(activeDraft.id, mailboxId);
      setActiveDraft(updated);
      setDrafts((prev) => prev.map((d) => (d.id === updated.id ? updated : d)));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to set sending mailbox");
    }
  }

  async function handleSendDraft() {
    if (!activeDraft) return;
    setSending(true);
    setError(null);
    try {
      const updated = await api.sendDraft(activeDraft.id, activeMailboxId || undefined);
      setActiveDraft(updated);
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to send email");
    } finally {
      setSending(false);
    }
  }

  async function handleSendAllApproved() {
    setSending(true);
    setError(null);
    try {
      const result = await api.sendApprovedDrafts(sendFrom || undefined);
      const failed = result.results.filter((r) => r.status === "error");
      if (failed.length) setError(`Some sends failed: ${failed.map((f) => f.error).join("; ")}`);
      setActiveDraft(null);
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Bulk send failed");
    } finally {
      setSending(false);
    }
  }

  async function handleInboxSync() {
    setError(null);
    try {
      const run = await api.startInboxSync(sendFrom || undefined);
      setSync(run);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Inbox sync failed to start");
    }
  }

  const approvedDraftCount = drafts.filter((d) => d.status === "approved").length;
  // The draft's own saved identity wins; otherwise fall back to the page-level pick.
  const activeMailboxId = activeDraft?.sending_mailbox_id || sendFrom;
  const activeMailbox = mailboxes.find((m) => m.id === activeMailboxId) || null;
  const sendableMailboxes = mailboxes.filter((m) => m.can_send);
  // The backend reports readiness per transport; app-only and Gmail never need a sign-in.
  const blockedMailbox = activeMailbox != null && !activeMailbox.can_send;

  return (
    <main className="page">
      <div className="header">
        <div>
          <Nav />
          <h1 style={{ marginTop: 12 }}>Fundraising Outreach</h1>
          <p>
            Draft and send emails to approved contacts via Outlook
            {auth?.connected && auth.user_email ? ` · ${auth.user_email}` : ""}
          </p>
        </div>
        <div className="actions">
          <button onClick={handleInboxSync} disabled={sync?.status === "running"}>
            {sync?.status === "running" && sync.sync_type === "inbox"
              ? "Syncing inbox…"
              : "Sync Inbox (replies)"}
          </button>
          {/* Shown only for a mailbox that genuinely cannot avoid the interactive sign-in. */}
          {mailboxes.some((m) => m.needs_signin) && (
            <a
              className="button"
              href={api.loginUrl()}
              title={mailboxes.find((m) => m.needs_signin)?.detail}
            >
              Sign in to {mailboxes.find((m) => m.needs_signin)?.from_email}
            </a>
          )}
        </div>
      </div>

      {error && <div className="banner error">{error}</div>}
      {sync?.status === "running" && (
        <div className="banner info">
          {sync.sync_type === "inbox" ? "Inbox" : "Sent"} sync: {sync.messages_fetched.toLocaleString()} fetched…
        </div>
      )}

      {mailboxes.length > 0 && (
        <div className={`banner ${sendableMailboxes.length === mailboxes.length ? "success" : "info"}`}>
          <strong>
            {sendableMailboxes.length} of {mailboxes.length} mailboxes ready to send
          </strong>
          {sendableMailboxes.length > 0 && `: ${sendableMailboxes.map((m) => m.from_email).join(", ")}. `}
          Pick the sending identity per draft below. Use <strong>Sync Inbox</strong> to track who has
          not replied.
        </div>
      )}

      {mailboxes.length > 0 && (
        <div className="outreach-panel" style={{ marginTop: 16 }}>
          <div className="panel-header">
            <h2>Sending mailboxes ({mailboxes.length})</h2>
          </div>
          <div className="mailbox-strip">
            {mailboxes.map((m) => (
              <div key={m.id} className={`mailbox-card${m.can_send ? " ready" : " blocked"}`}>
                <strong>{m.label}</strong>
                <span className="email">{m.from_email}</span>
                <span className={`draft-status ${m.can_send ? "approved" : "error"}`}>
                  {m.can_send ? "Ready to send" : "Cannot send yet"}
                </span>
                <span className="meta">{m.detail}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="outreach-layout">
        <div className="outreach-panel">
          <div className="panel-header">
            <h2>Approved contacts ({contacts.length})</h2>
            <select value={notRepliedDays} onChange={(e) => setNotRepliedDays(e.target.value)}>
              <option value="">All approved</option>
              <option value="2">No reply ≥ 2 days</option>
              <option value="3">No reply ≥ 3 days</option>
              <option value="7">No reply ≥ 7 days</option>
              <option value="14">No reply ≥ 14 days</option>
              <option value="30">No reply ≥ 30 days</option>
            </select>
          </div>

          {loading ? (
            <p>Loading…</p>
          ) : contacts.length === 0 ? (
            <p className="drawer-empty">No approved contacts. Approve people on the Contacts page first.</p>
          ) : (
            <div className="contact-select-list">
              <label className="select-all">
                <input
                  type="checkbox"
                  checked={selected.size === contacts.length && contacts.length > 0}
                  onChange={toggleAll}
                />
                Select all ({selected.size} selected)
              </label>
              {contacts.map((c) => (
                <label key={c.id} className={`contact-select-item${selected.has(c.id) ? " selected" : ""}`}>
                  <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggleContact(c.id)} />
                  <span className="serial">#{c.list_number}</span>
                  <span className="name">{c.full_name || c.primary_email}</span>
                  <span className="email">{c.primary_email}</span>
                  {c.awaiting_reply && c.days_since_outreach != null && (
                    <span className="reply-badge">No reply {c.days_since_outreach}d</span>
                  )}
                </label>
              ))}
            </div>
          )}
        </div>

        <div className="outreach-panel outreach-compose">
          <div className="panel-header">
            <h2>Compose with AI</h2>
            <button onClick={() => setShowPromptEditor(!showPromptEditor)}>
              {showPromptEditor ? "Hide prompt template" : "Edit prompt template"}
            </button>
          </div>

          {showPromptEditor && (
            <div className="prompt-editor">
              <label>
                System prompt (instructions for the LLM)
                <textarea rows={3} value={systemPrompt} onChange={(e) => setSystemPrompt(e.target.value)} />
              </label>
              <label>
                User prompt template — use {"{context}"} and {"{custom_instructions_block}"} placeholders
                <textarea rows={10} value={userTemplate} onChange={(e) => setUserTemplate(e.target.value)} />
              </label>
              <button onClick={savePromptTemplate}>Save prompt template</button>
              {prompt?.updated_at && (
                <small className="meta">Last saved {formatDate(prompt.updated_at)}</small>
              )}
            </div>
          )}

          <label>
            Objective — what are you trying to get out of this outreach?
            <input
              placeholder="e.g. board seat, Series A raise, distribution partner…"
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
            />
            <small className="meta">
              Used to judge who matters and why. Left blank, drafts fall back to general relationship
              context.
            </small>
          </label>

          <div className="actions">
            <button onClick={handlePrioritize} disabled={ranking || !objective.trim()}>
              {ranking ? "Ranking…" : "Prioritize contacts for this objective"}
            </button>
            {ranked && (
              <button className="link-btn" onClick={() => setRanked(null)}>
                Clear ranking
              </button>
            )}
          </div>

          {ranked && (
            <div className="ranked-list">
              <strong className="picker-label">
                Ranked for “{objective}” — best first
              </strong>
              {ranked.map((r, i) => (
                <label
                  key={r.contact_id}
                  className={`ranked-item${selected.has(r.contact_id) ? " selected" : ""}`}
                >
                  <input
                    type="checkbox"
                    checked={selected.has(r.contact_id)}
                    onChange={() => toggleContact(r.contact_id)}
                  />
                  <span className="rank">{i + 1}</span>
                  <span className={`score${r.objective_score === null ? " unscored" : ""}`}>
                    {r.objective_score === null ? "—" : r.objective_score}
                  </span>
                  <span className="who">
                    <strong>{r.full_name || r.primary_email}</strong>
                    {r.company_name ? ` · ${r.company_name}` : ""}
                    {r.review_status !== "approved" && (
                      <em className="not-approved"> (approve before drafting)</em>
                    )}
                    <span className="why">{r.reason || "No score returned for this contact."}</span>
                  </span>
                </label>
              ))}
            </div>
          )}

          <label>
            Optional instructions for this batch (tone, angle, specifics)
            <textarea
              rows={3}
              placeholder="e.g. Mention our Q3 Galaxy Pharma deck and ask for a 15-min call…"
              value={customInstructions}
              onChange={(e) => setCustomInstructions(e.target.value)}
            />
          </label>

          <div className="actions">
            <button className="primary" onClick={handleGenerate} disabled={generating}>
              {generating
                ? "Generating…"
                : selected.size > 1
                  ? `Generate ${selected.size} drafts`
                  : selected.size === 1
                    ? "Generate draft"
                    : "Generate draft (select contacts)"}
            </button>
          </div>
        </div>
      </div>

      <div className="outreach-panel" style={{ marginTop: 16 }}>
        <div className="panel-header">
          <h2>Drafts ({drafts.length})</h2>
          <div className="actions">
            {sendableMailboxes.length > 1 && (
              <select
                value={sendFrom}
                onChange={(e) => setSendFrom(e.target.value)}
                title="Default sending mailbox for drafts that do not have one set"
              >
                {sendableMailboxes.map((m) => (
                  <option key={m.id} value={m.id}>
                    Send from: {m.from_email}
                  </option>
                ))}
              </select>
            )}
            {approvedDraftCount > 0 && (
              <button
                className="primary"
                onClick={handleSendAllApproved}
                disabled={sending || sendableMailboxes.length === 0}
              >
                Send all approved ({approvedDraftCount})
              </button>
            )}
          </div>
        </div>

        <div className="drafts-layout">
          <div className="draft-list">
            {drafts.length === 0 ? (
              <p className="drawer-empty">No drafts yet. Select contacts and generate.</p>
            ) : (
              drafts.map((d) => (
                <button
                  key={d.id}
                  className={`draft-list-item${activeDraft?.id === d.id ? " active" : ""}`}
                  onClick={() => setActiveDraft(d)}
                >
                  <strong>#{d.list_number} {d.contact_name}</strong>
                  <span>{d.subject || "(no subject)"}</span>
                  <span className={`draft-status ${d.status}`}>{d.status}</span>
                </button>
              ))
            )}
          </div>

          {activeDraft && (
            <div className="draft-editor">
              <h3>
                #{activeDraft.list_number} · {activeDraft.contact_name} · {activeDraft.contact_email}
              </h3>
              <label>
                Subject
                <input
                  value={activeDraft.subject || ""}
                  onChange={(e) => setActiveDraft({ ...activeDraft, subject: e.target.value })}
                />
              </label>
              <label>
                Body
                <textarea
                  rows={12}
                  value={activeDraft.body || ""}
                  onChange={(e) => setActiveDraft({ ...activeDraft, body: e.target.value })}
                />
              </label>

              <button type="button" className="link-btn" onClick={() => setShowPromptUsed(!showPromptUsed)}>
                {showPromptUsed ? "Hide" : "Show"} prompt sent to LLM
              </button>
              {showPromptUsed && (
                <div className="prompt-used">
                  <h4>System prompt</h4>
                  <pre>{activeDraft.system_prompt}</pre>
                  <h4>User prompt</h4>
                  <pre>{activeDraft.user_prompt}</pre>
                  {activeDraft.custom_instructions && (
                    <>
                      <h4>Your custom instructions</h4>
                      <pre>{activeDraft.custom_instructions}</pre>
                    </>
                  )}
                </div>
              )}

              <MailboxPicker
                mailboxes={mailboxes}
                value={activeMailboxId}
                onChange={handleChooseMailbox}
                label="Send from"
                capability="send"
              />

              <div className="actions">
                <button onClick={handleSaveDraft}>Save edits</button>
                <button onClick={handleApproveDraft} disabled={activeDraft.status === "approved"}>
                  Approve draft
                </button>
                <button
                  className="primary"
                  onClick={handleSendDraft}
                  disabled={sending || sendableMailboxes.length === 0 || blockedMailbox}
                >
                  {sending
                    ? "Sending…"
                    : activeMailbox
                      ? `Send as ${activeMailbox.from_email}`
                      : "Send"}
                </button>
              </div>
              {activeDraft.error_message && (
                <div className="banner error" style={{ marginTop: 8 }}>
                  {activeDraft.error_message}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
