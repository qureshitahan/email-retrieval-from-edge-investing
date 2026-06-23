"use client";

import { useCallback, useEffect, useState } from "react";
import { Nav } from "@/components/Nav";
import {
  api,
  Contact,
  EmailDraft,
  formatDate,
  OutreachPrompt,
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
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [activeDraft, setActiveDraft] = useState<EmailDraft | null>(null);
  const [prompt, setPrompt] = useState<OutreachPrompt | null>(null);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [userTemplate, setUserTemplate] = useState("");
  const [customInstructions, setCustomInstructions] = useState("");
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
      const [authStatus, contactData, draftData, promptData] = await Promise.all([
        api.authStatus(),
        api.contacts(params),
        api.listDrafts(),
        api.getOutreachPrompt(),
      ]);
      setAuth(authStatus);
      setContacts(contactData.items);
      setDrafts(draftData.items.filter((d) => d.status !== "sent"));
      setPrompt(promptData);
      setSystemPrompt(promptData.system_prompt);
      setUserTemplate(promptData.user_prompt_template);
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
      const status = await api.syncStatus();
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
      const result = await api.generateDrafts(ids, customInstructions || undefined);
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

  async function handleSendDraft() {
    if (!activeDraft) return;
    setSending(true);
    setError(null);
    try {
      const updated = await api.sendDraft(activeDraft.id);
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
      const result = await api.sendApprovedDrafts();
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
      const run = await api.startInboxSync();
      setSync(run);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Inbox sync failed to start");
    }
  }

  const approvedDraftCount = drafts.filter((d) => d.status === "approved").length;

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
          {!auth?.connected ? (
            <a className="button primary" href={api.loginUrl()}>
              Connect Microsoft Outlook
            </a>
          ) : (
            <>
              <a className="button" href={api.loginUrl()} title="Reconnect after adding Mail.Send in Azure">
                Reconnect Outlook
              </a>
              <button onClick={handleInboxSync} disabled={sync?.status === "running"}>
                {sync?.status === "running" && sync.sync_type === "inbox" ? "Syncing inbox…" : "Sync Inbox (replies)"}
              </button>
            </>
          )}
        </div>
      </div>

      {error && <div className="banner error">{error}</div>}
      {sync?.status === "running" && (
        <div className="banner info">
          {sync.sync_type === "inbox" ? "Inbox" : "Sent"} sync: {sync.messages_fetched.toLocaleString()} fetched…
        </div>
      )}

      {auth?.connected && auth.can_send_mail && (
        <div className="banner success">
          Outlook connected with send permission. You can generate drafts and send emails from here. Use{" "}
          <strong>Sync Inbox</strong> to track who has not replied.
        </div>
      )}

      {auth?.connected && !auth.can_send_mail && (
        <div className="banner error">
          Connected as {auth.user_email}, but <strong>Mail.Send</strong> is not in your session yet. Click{" "}
          <strong>Reconnect Outlook</strong>, sign in as dbains@edgeinvesting.ca, and accept all permissions. If
          it still fails, restart the backend and reconnect again.
        </div>
      )}

      {!auth?.connected && (
        <div className="banner info">
          Connect Microsoft Outlook to draft and send fundraising emails.
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
            {approvedDraftCount > 0 && (
              <button className="primary" onClick={handleSendAllApproved} disabled={sending}>
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

              <div className="actions">
                <button onClick={handleSaveDraft}>Save edits</button>
                <button onClick={handleApproveDraft} disabled={activeDraft.status === "approved"}>
                  Approve draft
                </button>
                <button className="primary" onClick={handleSendDraft} disabled={sending}>
                  {sending ? "Sending…" : "Send via Outlook"}
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
