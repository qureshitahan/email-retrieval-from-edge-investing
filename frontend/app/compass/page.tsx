"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { DraftCard } from "@/components/DraftCard";
import { Nav } from "@/components/Nav";
import {
  api,
  DraftRun,
  EmailDraft,
  MailboxStatus,
  ObjectivePlan,
  RankedContact,
} from "@/lib/api";

/**
 * Objective-first outreach.
 *
 * The contact list is not the starting point — you state a goal, pick which mailboxes to
 * search, and the tool finds the people who matter for that goal across the whole history.
 * Approval happens in bulk on the resulting shortlist, which is the step that made the
 * old "review thousands of contacts one by one" flow unusable.
 */

const QUICK_STARTS: Array<{ label: string; objective: string }> = [
  { label: "Fundraising help", objective: "Raise capital — investors who could fund or open doors" },
  { label: "Customer intros", objective: "Warm introductions to potential customers" },
  { label: "Reconnect", objective: "Reconnect with valuable people who have gone quiet" },
  { label: "Find an expert", objective: "Find subject-matter experts who can advise us" },
  { label: "Find partners", objective: "Find distribution or commercial partners" },
  { label: "Neglected VIPs", objective: "Important relationships we have not nurtured recently" },
  { label: "Board seat", objective: "Find people who could help secure a board seat" },
];

/** Only a draft that actually generated can be reviewed and sent. */
function isSendable(draft: EmailDraft) {
  return draft.status !== "sent" && draft.status !== "failed";
}

export default function CompassPage() {
  const [mailboxes, setMailboxes] = useState<MailboxStatus[]>([]);
  const [included, setIncluded] = useState<Set<string>>(new Set());
  const [objective, setObjective] = useState("");
  const [scanDepth, setScanDepth] = useState(200);
  const [shortlistSize, setShortlistSize] = useState(25);

  const [ranked, setRanked] = useState<RankedContact[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [openEvidence, setOpenEvidence] = useState<Set<string>>(new Set());
  // The answers that turn a one-line objective into a filter. Proposed by the model, edited
  // by the user, and scored against — so "investors" can mean what Dalbir means by it.
  const [plan, setPlan] = useState<ObjectivePlan | null>(null);
  const [planning, setPlanning] = useState(false);
  // mailbox id -> the signature appended to its drafts, so the line count excludes it.
  const [signatures, setSignatures] = useState<Record<string, string>>({});
  const [summary, setSummary] = useState<string | null>(null);

  const [drafts, setDrafts] = useState<EmailDraft[]>([]);
  const [selectedDrafts, setSelectedDrafts] = useState<Set<string>>(new Set());
  const [draftRun, setDraftRun] = useState<DraftRun | null>(null);
  // Which run the poll loop is following, so a second one cannot start alongside it and a
  // navigation away can stop it.
  const draftPollRef = useRef<string | null>(null);

  const [loading, setLoading] = useState(true);
  const [searching, setSearching] = useState(false);
  const [approving, setApproving] = useState(false);
  const [drafting, setDrafting] = useState(false);
  const [sending, setSending] = useState(false);
  const [busyDraft, setBusyDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.mailboxStatuses();
      setMailboxes(data.items);
      // Search every readable mailbox by default — narrowing is the exception.
      setIncluded((prev) =>
        prev.size > 0 ? prev : new Set(data.items.filter((m) => m.can_read).map((m) => m.id))
      );
      setError(data.config_error);
      try {
        const senders = await api.senders();
        setSignatures(
          Object.fromEntries(
            senders.items.map((p) => [p.mailbox_id, p.effective_signature || ""])
          )
        );
      } catch {
        // A missing profile only affects the line count hint, never the draft itself.
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load mailboxes");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // A run started before a reload is still going on the server. Attach to it rather than
  // leaving the page looking idle while emails are being written — and pick up the drafts of
  // a run that finished while the tab was closed.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const run = await api.latestDraftRun();
        if (cancelled || !run) return;
        if (run.items?.length) {
          setDrafts(run.items);
          setSelectedDrafts(new Set(run.items.filter(isSendable).map((d) => d.id)));
        }
        setDraftRun(run);
        if (run.status === "running") followDraftRun(run.id);
      } catch {
        // Nothing in flight, or the API is not up yet. Neither is worth an error banner.
      }
    })();
    return () => {
      cancelled = true;
      draftPollRef.current = null;
    };
    // Mount only: re-running this on every objective keystroke would restart the poll loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function toggleMailbox(id: string) {
    setIncluded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleEvidence(id: string) {
    setOpenEvidence((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleContact(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function handlePlan() {
    if (!objective.trim()) {
      setError("Describe what you want to accomplish first");
      return;
    }
    setPlanning(true);
    setError(null);
    try {
      const result = await api.objectivePlan(objective.trim());
      setPlan(result);
      if (result.questions.length === 0) {
        // No questions worth asking — go straight to the search rather than stalling.
        await handleContinue(result);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not plan this objective");
    } finally {
      setPlanning(false);
    }
  }

  function editAnswer(index: number, answer: string) {
    setPlan((prev) =>
      prev
        ? { ...prev, questions: prev.questions.map((q, i) => (i === index ? { ...q, answer } : q)) }
        : prev
    );
  }

  async function handleContinue(usePlan: ObjectivePlan | null = plan) {
    if (!objective.trim()) {
      setError("Describe what you want to accomplish first");
      return;
    }
    if (included.size === 0) {
      setError("Choose at least one mailbox to search");
      return;
    }
    setSearching(true);
    setError(null);
    setNotice(null);
    setRanked(null);
    try {
      const result = await api.prioritize(
        objective.trim(),
        [],
        scanDepth,
        shortlistSize,
        Array.from(included),
        usePlan
      );
      setRanked(result.items);
      setSelected(new Set(result.items.map((r) => r.contact_id)));
      setSummary(
        `Searched ${result.scanned.toLocaleString()} relationships across ${included.size} mailbox(es)` +
          `, showing the top ${result.items.length}` +
          (result.failed_batches ? ` — ${result.failed_batches} batch(es) failed` : "")
      );
      if (result.items.length === 0) {
        setError(
          "Nobody came back for this objective. Try a broader goal, a deeper scan, or more mailboxes."
        );
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Search failed");
    } finally {
      setSearching(false);
    }
  }

  async function handleApprove() {
    const ids = (ranked || []).filter((r) => selected.has(r.contact_id)).map((r) => r.contact_id);
    if (ids.length === 0) {
      setError("Tick at least one person");
      return;
    }
    setApproving(true);
    setError(null);
    try {
      const result = await api.bulkReview(ids, "approved");
      setRanked((prev) =>
        prev
          ? prev.map((r) => (ids.includes(r.contact_id) ? { ...r, review_status: "approved" } : r))
          : prev
      );
      setNotice(`Approved ${result.updated} people. You can draft to them now.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Bulk approve failed");
    } finally {
      setApproving(false);
    }
  }

  async function handleDraft() {
    const chosen = (ranked || []).filter((r) => selected.has(r.contact_id));
    const unapproved = chosen.filter((r) => r.review_status !== "approved");
    if (chosen.length === 0) {
      setError("Tick at least one person");
      return;
    }
    if (unapproved.length > 0) {
      setError(`Approve the ${unapproved.length} unapproved person(s) first — drafting is gated on it.`);
      return;
    }
    setDrafting(true);
    setError(null);
    try {
      const run = await api.startDrafting(
        chosen.map((r) => r.contact_id),
        undefined,
        objective.trim(),
        // Pass the searched mailboxes so each draft is pinned to the one that already
        // corresponds with its recipient.
        Array.from(included),
        chosen.map((r) => ({
          contact_id: r.contact_id,
          reason: r.reason,
          score: r.objective_score,
        }))
      );
      if (run.already_running) {
        setNotice("A drafting run was already going — showing its progress.");
      }
      await followDraftRun(run.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Drafting failed");
      setDrafting(false);
    }
  }

  /**
   * Watch a drafting run to completion, showing each email as it is written.
   *
   * Nothing here holds a long request open. That is the point: a batch of forty takes several
   * minutes, and Azure closes any connection idle for 230 seconds, which used to surface as
   * "Failed to fetch" on precisely the large batches this flow is built to produce. Each poll
   * is its own short request, so batch size no longer has anything to do with it.
   */
  const followDraftRun = useCallback(
    async (runId: string) => {
      setDrafting(true);
      draftPollRef.current = runId;
      let misses = 0;

      while (draftPollRef.current === runId) {
        let run: DraftRun;
        try {
          run = await api.draftRun(runId);
          misses = 0;
        } catch {
          // A dropped poll is not a failed run — the work continues on the server. Keep
          // trying for a while before giving up on watching it.
          misses += 1;
          if (misses >= 5) {
            setError("Lost contact with the server. The drafts are still being written — reload to pick them up.");
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 3000));
          continue;
        }

        setDraftRun(run);
        if (run.items) {
          setDrafts(run.items);
          setSelectedDrafts(new Set(run.items.filter(isSendable).map((d) => d.id)));
        }

        if (run.status !== "running") {
          const failed = run.failed;
          if (run.status === "failed" && run.error_message) {
            setError(run.error_message);
          } else {
            setNotice(
              `Drafted ${run.completed} email(s) for "${run.objective || objective.trim()}". ` +
                `Review them below, then send.` +
                (failed ? ` ${failed} could not be drafted.` : "")
            );
          }
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }

      if (draftPollRef.current === runId) {
        draftPollRef.current = null;
        setDrafting(false);
      }
    },
    [objective]
  );

  function patchDraft(id: string, patch: Partial<EmailDraft>) {
    setDrafts((prev) => prev.map((d) => (d.id === id ? { ...d, ...patch } : d)));
  }

  async function handleDraftMailbox(id: string, mailboxId: string) {
    patchDraft(id, { sending_mailbox_id: mailboxId });
    try {
      await api.setDraftMailbox(id, mailboxId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not change the sending mailbox");
    }
  }

  async function handleSaveDraft(id: string) {
    const draft = drafts.find((d) => d.id === id);
    if (!draft) return;
    try {
      await api.updateDraft(id, { subject: draft.subject || "", body: draft.body || "" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the draft");
    }
  }

  async function handleRegenerate(id: string) {
    const draft = drafts.find((d) => d.id === id);
    if (!draft) return;
    setBusyDraft(id);
    setError(null);
    try {
      const fresh = await api.generateDraftForContact(draft.contact_id, undefined, objective.trim());
      patchDraft(id, { subject: fresh.subject, body: fresh.body, status: fresh.status });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Regenerate failed");
    } finally {
      setBusyDraft(null);
    }
  }

  async function handleDiscard(id: string) {
    setBusyDraft(id);
    try {
      await api.discardDraft(id);
      setDrafts((prev) => prev.filter((d) => d.id !== id));
      setSelectedDrafts((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not discard the draft");
    } finally {
      setBusyDraft(null);
    }
  }

  async function handleSendOne(id: string) {
    setBusyDraft(id);
    setError(null);
    try {
      await handleSaveDraft(id);
      const sent = await api.sendDraft(id);
      patchDraft(id, { status: sent.status, sent_at: sent.sent_at });
      setNotice(`Sent to ${sent.contact_email}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Send failed");
    } finally {
      setBusyDraft(null);
    }
  }

  async function handleSendAll() {
    const ids = drafts.filter((d) => selectedDrafts.has(d.id) && isSendable(d)).map((d) => d.id);
    if (ids.length === 0) {
      setError("No unsent drafts are selected");
      return;
    }
    const missing = drafts.filter((d) => ids.includes(d.id) && !d.sending_mailbox_id);
    if (missing.length > 0) {
      setError(`${missing.length} draft(s) have no sending mailbox. Choose one on each card.`);
      return;
    }
    setSending(true);
    setError(null);
    try {
      // Save any edits first, or the sent copy would not match what is on screen.
      await Promise.all(ids.map((id) => handleSaveDraft(id)));
      // No mailbox_id: each draft goes out from the identity it was pinned to.
      const result = await api.sendDraftBatch(ids);
      setDrafts((prev) =>
        prev.map((d) =>
          result.results.some((r) => r.draft_id === d.id && r.status === "sent")
            ? { ...d, status: "sent" }
            : d
        )
      );
      const breakdown = Object.entries(result.by_mailbox)
        .map(([id, n]) => {
          const box = mailboxes.find((m) => m.id === id);
          return `${n} from ${box ? box.from_email : id}`;
        })
        .join(", ");
      setNotice(
        `Sent ${result.sent} email(s)${breakdown ? ` — ${breakdown}` : ""}.` +
          (result.failed ? ` ${result.failed} failed.` : "")
      );
      if (result.failed) {
        const firstError = result.results.find((r) => r.status === "error");
        if (firstError?.error) setError(`First failure: ${firstError.error}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Send all failed");
    } finally {
      setSending(false);
    }
  }

  const readable = mailboxes.filter((m) => m.can_read);
  const connected = mailboxes.filter((m) => m.connected).length;
  const approvedCount = (ranked || []).filter((r) => r.review_status === "approved").length;

  return (
    <main className="page compass">
      <div className="header">
        <div>
          <Nav />
          <h1 style={{ marginTop: 12 }}>
            Compass <span className="compass-sub">your relationship agent</span>
          </h1>
        </div>
        <div className="actions">
          <span className="pill-stat">{connected} connected</span>
          <span className="pill-stat">{included.size} included</span>
        </div>
      </div>

      <h2 className="compass-question">
        What would you like to accomplish through your relationships?
      </h2>
      <p className="compass-lead">
        Describe your goal, choose mailboxes, and continue — Compass ranks real relationships from
        your synced history. You do not need to approve anyone first.
      </p>

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner success">{notice}</div>}

      <div className="compass-layout">
        <div className="outreach-panel compass-side">
          <div className="panel-header">
            <h2>Shortlist</h2>
            <span className="meta">{ranked ? `${ranked.length} found` : "none yet"}</span>
          </div>
          {!ranked ? (
            <p className="drawer-empty">
              No shortlist yet — describe an objective and press Continue.
            </p>
          ) : (
            <>
              <p className="meta">{summary}</p>
              <p className="meta">
                <strong>{selected.size}</strong> selected · <strong>{approvedCount}</strong> approved
              </p>
            </>
          )}
        </div>

        <div className="outreach-panel">
          <label className="field-label" htmlFor="objective">
            OBJECTIVE
          </label>
          <textarea
            id="objective"
            rows={3}
            placeholder="e.g. Help with Galaxy Pharma's capital raise"
            value={objective}
            onChange={(e) => setObjective(e.target.value)}
          />

          <div className="field-label" style={{ marginTop: 14 }}>
            QUICK STARTS
          </div>
          <div className="quick-starts">
            {QUICK_STARTS.map((q) => (
              <button
                key={q.label}
                type="button"
                className={`quick-chip${objective === q.objective ? " active" : ""}`}
                onClick={() => setObjective(q.objective)}
              >
                {q.label}
              </button>
            ))}
          </div>

          <div className="scan-controls" style={{ marginTop: 14 }}>
            <label>
              How deep to search
              <select value={scanDepth} onChange={(e) => setScanDepth(Number(e.target.value))}>
                <option value={100}>100 relationships (fastest)</option>
                <option value={200}>200 relationships</option>
                <option value={400}>400 relationships</option>
                <option value={600}>600 relationships (deepest)</option>
              </select>
            </label>
            <label>
              Shortlist size
              <select
                value={shortlistSize}
                onChange={(e) => setShortlistSize(Number(e.target.value))}
              >
                <option value={10}>Top 10 people</option>
                <option value={25}>Top 25 people</option>
                <option value={50}>Top 50 people</option>
                <option value={100}>Top 100 people</option>
              </select>
            </label>
          </div>

          <button
            className="button primary continue-btn"
            onClick={handlePlan}
            disabled={planning || searching || !objective.trim() || included.size === 0}
          >
            {planning
              ? "Thinking it through…"
              : searching
                ? "Finding your people…"
                : "Plan with AI"}
          </button>

          {plan && plan.questions.length > 0 && (
            /* The objective alone is too coarse to rank on: "investors" means something
               specific to the person typing it. Every answer here is already filled in, so
               the fast path is to read them and press the button. */
            <div className="plan-box">
              <div className="plan-head">
                <strong>Before I search — is this what you mean?</strong>
                <span className="meta">Answers are filled in for you. Edit any of them.</span>
              </div>

              {plan.looking_for && (
                <p className="meta plan-summary">Looking for: {plan.looking_for}</p>
              )}
              {plan.avoid && <p className="meta plan-summary">Leaving out: {plan.avoid}</p>}

              {plan.questions.map((q, i) => (
                <label key={i} className="plan-question">
                  <span className="plan-q">{q.question}</span>
                  <textarea
                    rows={2}
                    value={q.answer}
                    onChange={(e) => editAnswer(i, e.target.value)}
                  />
                  {q.why && <span className="meta">{q.why}</span>}
                </label>
              ))}

              <div className="actions">
                <button
                  className="primary"
                  onClick={() => handleContinue()}
                  disabled={searching || included.size === 0}
                >
                  {searching ? "Finding your people…" : `Find my ${shortlistSize} people`}
                </button>
                <button className="link-btn" onClick={() => setPlan(null)} disabled={searching}>
                  Skip these
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="outreach-panel" style={{ marginTop: 16 }}>
        <div className="panel-header">
          <h2>Where should I look?</h2>
          <span className="meta">
            {included.size}/{readable.length} selected
          </span>
        </div>
        {loading ? (
          <p className="meta">Loading mailboxes…</p>
        ) : mailboxes.length === 0 ? (
          <p className="drawer-empty">
            No mailboxes configured. Set <code>OUTREACH_MAILBOXES</code> in the backend
            <code>.env</code>.
          </p>
        ) : (
          <div className="mailbox-strip">
            {mailboxes.map((m) => {
              const usable = m.can_read;
              return (
                <label
                  key={m.id}
                  className={`mailbox-card${included.has(m.id) ? " ready" : " blocked"}`}
                  style={{ cursor: usable ? "pointer" : "not-allowed" }}
                >
                  <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <input
                      type="checkbox"
                      checked={included.has(m.id)}
                      disabled={!usable}
                      onChange={() => toggleMailbox(m.id)}
                    />
                    <strong>{m.from_email}</strong>
                  </span>
                  <span className="meta">{m.detail}</span>
                  {!usable && (
                    <span className="draft-status error">Cannot be searched yet</span>
                  )}
                </label>
              );
            })}
          </div>
        )}
      </div>

      {ranked && (
        <div className="outreach-panel" style={{ marginTop: 16 }}>
          <div className="panel-header">
            <h2>
              People for “{objective.trim()}” ({ranked.length})
            </h2>
            <div className="actions">
              <button
                className="link-btn"
                onClick={() => setSelected(new Set(ranked.map((r) => r.contact_id)))}
              >
                Select all
              </button>
              <button className="link-btn" onClick={() => setSelected(new Set())}>
                Select none
              </button>
            </div>
          </div>

          <div className="actions" style={{ marginBottom: 10 }}>
            <button className="primary" onClick={handleApprove} disabled={approving}>
              {approving ? "Approving…" : `Approve ${selected.size} selected`}
            </button>
            <button onClick={handleDraft} disabled={drafting || selected.size === 0}>
              {drafting ? "Drafting…" : `Draft ${selected.size} personalised emails`}
            </button>
          </div>

          {draftRun && (drafting || draftRun.status === "running") && (
            <div className="draft-progress">
              <div className="draft-progress-head">
                <span>
                  {draftRun.phase === "studying"
                    ? "Reading their recent mail…"
                    : `Writing ${draftRun.done + 1} of ${draftRun.total}`}
                  {draftRun.current_label && draftRun.phase === "writing" && (
                    <span className="meta"> · {draftRun.current_label}</span>
                  )}
                </span>
                <span className="meta">{draftRun.percent}%</span>
              </div>
              <div className="draft-progress-track">
                <div
                  className={`draft-progress-bar${draftRun.phase === "studying" ? " indeterminate" : ""}`}
                  style={{ width: `${Math.max(draftRun.percent, 3)}%` }}
                />
              </div>
              {draftRun.people && draftRun.people.length > 0 && (
                <ul className="draft-queue">
                  {draftRun.people.map((person) => (
                    <li key={person.contact_id} className={`draft-queue-item ${person.status}`}>
                      <span className="draft-queue-mark" aria-hidden="true">
                        {person.status === "done"
                          ? "✓"
                          : person.status === "failed"
                            ? "!"
                            : person.status === "writing"
                              ? "…"
                              : "○"}
                      </span>
                      <span className="draft-queue-name">{person.name}</span>
                      <span className="meta">
                        {person.status === "done"
                          ? "drafted"
                          : person.status === "writing"
                            ? "writing now"
                            : person.status === "failed"
                              ? person.error || "failed"
                              : person.status === "skipped"
                                ? "not drafted"
                                : "waiting"}
                      </span>
                    </li>
                  ))}
                </ul>
              )}

              <span className="meta">
                This keeps running if you close the tab — reopen Compass to pick it up.
                {draftRun.failed > 0 && ` ${draftRun.failed} could not be drafted so far.`}
              </span>
            </div>
          )}

          <div className="ranked-list">
            {ranked.map((r, i) => (
              <div
                key={r.contact_id}
                className={`ranked-item${selected.has(r.contact_id) ? " selected" : ""}`}
              >
                <label className="ranked-main">
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
                    {r.review_status === "approved" && <em className="approved-tag"> approved</em>}
                    <span className="why">{r.reason || "No score returned for this contact."}</span>
                  </span>
                </label>
                {r.evidence && (
                  <div className="ranked-evidence">
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => toggleEvidence(r.contact_id)}
                    >
                      {openEvidence.has(r.contact_id) ? "Hide evidence" : "Show evidence"}
                    </button>
                    {openEvidence.has(r.contact_id) && (
                      /* Exactly what the ranker was shown. A wrong pick is only obvious next
                         to the evidence it was picked on. */
                      <pre className="evidence-block">{r.evidence}</pre>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {drafts.length > 0 && (
        <div className="outreach-panel" style={{ marginTop: 16 }}>
          <div className="panel-header">
            <h2>
              Review drafts ({drafts.filter(isSendable).length} to send
              {drafts.some((d) => d.status === "sent") &&
                `, ${drafts.filter((d) => d.status === "sent").length} sent`}
              )
            </h2>
            <div className="actions">
              <button
                className="link-btn"
                onClick={() => setSelectedDrafts(new Set(drafts.filter(isSendable).map((d) => d.id)))}
              >
                Select all
              </button>
              <button className="link-btn" onClick={() => setSelectedDrafts(new Set())}>
                Select none
              </button>
            </div>
          </div>

          <p className="meta">
            Each draft is pre-set to send from the mailbox that already corresponds with that
            person. Change any of them below, or send them all as they stand.
          </p>

          <div className="actions send-all-row">
            <button className="primary" onClick={handleSendAll} disabled={sending}>
              {sending
                ? "Sending…"
                : `Send draft to all (${
                    drafts.filter((d) => selectedDrafts.has(d.id) && isSendable(d)).length
                  })`}
            </button>
          </div>

          <div className="draft-cards">
            {drafts.map((draft, i) => (
              <DraftCard
                key={draft.id}
                draft={draft}
                index={i}
                total={drafts.length}
                mailboxes={mailboxes}
                signature={signatures[draft.sending_mailbox_id || ""] || ""}
                selected={selectedDrafts.has(draft.id)}
                busy={busyDraft === draft.id || sending}
                onToggle={() =>
                  setSelectedDrafts((prev) => {
                    const next = new Set(prev);
                    if (next.has(draft.id)) next.delete(draft.id);
                    else next.add(draft.id);
                    return next;
                  })
                }
                onChangeMailbox={(mailboxId) => handleDraftMailbox(draft.id, mailboxId)}
                onEdit={(patch) => patchDraft(draft.id, patch)}
                onSend={() => handleSendOne(draft.id)}
                onRegenerate={() => handleRegenerate(draft.id)}
                onDelete={() => handleDiscard(draft.id)}
              />
            ))}
          </div>
        </div>
      )}
    </main>
  );
}
