"use client";

import { useCallback, useEffect, useState } from "react";
import { Nav } from "@/components/Nav";
import { api, MailboxStatus, RankedContact } from "@/lib/api";

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

export default function CompassPage() {
  const [mailboxes, setMailboxes] = useState<MailboxStatus[]>([]);
  const [included, setIncluded] = useState<Set<string>>(new Set());
  const [objective, setObjective] = useState("");
  const [scanDepth, setScanDepth] = useState(200);
  const [shortlistSize, setShortlistSize] = useState(25);

  const [ranked, setRanked] = useState<RankedContact[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [summary, setSummary] = useState<string | null>(null);

  const [loading, setLoading] = useState(true);
  const [searching, setSearching] = useState(false);
  const [approving, setApproving] = useState(false);
  const [drafting, setDrafting] = useState(false);
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
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load mailboxes");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function toggleMailbox(id: string) {
    setIncluded((prev) => {
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

  async function handleContinue() {
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
        Array.from(included)
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
      const result = await api.generateDrafts(
        chosen.map((r) => r.contact_id),
        undefined,
        objective.trim()
      );
      const failed = result.results?.filter((r) => r.status === "error") || [];
      setNotice(
        `Drafted ${result.items.length} email(s) for "${objective.trim()}". ` +
          `Open the Outreach tab to review, choose a sending mailbox, and send.` +
          (failed.length ? ` ${failed.length} failed.` : "")
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Drafting failed");
    } finally {
      setDrafting(false);
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
            onClick={handleContinue}
            disabled={searching || !objective.trim() || included.size === 0}
          >
            {searching ? "Finding your people…" : "Continue"}
          </button>
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

          <div className="ranked-list">
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
                  {r.review_status === "approved" && <em className="approved-tag"> approved</em>}
                  <span className="why">{r.reason || "No score returned for this contact."}</span>
                </span>
              </label>
            ))}
          </div>
        </div>
      )}
    </main>
  );
}
