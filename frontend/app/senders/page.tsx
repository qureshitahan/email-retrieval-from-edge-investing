"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Nav } from "@/components/Nav";
import { api, ProofPoint, SenderProfile } from "@/lib/api";

/**
 * Who is writing, per mailbox.
 *
 * Every other screen is about the recipient. This one is about the sender, because a draft
 * that knows everything about the person it is addressed to and nothing about the person
 * sending it opens well and then pitches like a form letter.
 *
 * Proof points are extracted from uploaded documents and each keeps the sentence it came from,
 * so a claim that ends up in a real email can be traced back to the résumé that supports it.
 */
export default function SendersPage() {
  const [profiles, setProfiles] = useState<SenderProfile[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [profile, setProfile] = useState<SenderProfile | null>(null);
  const [extensions, setExtensions] = useState<string[]>([]);

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const loadList = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.senders();
      setProfiles(data.items);
      setExtensions(data.supported_extensions);
      setSelected((prev) => prev ?? data.items[0]?.mailbox_id ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load sender profiles");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadList();
  }, [loadList]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await api.sender(selected);
        if (!cancelled) setProfile(data);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Could not load that profile");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selected]);

  function patch(field: keyof SenderProfile, value: string) {
    setProfile((prev) => (prev ? { ...prev, [field]: value } : prev));
  }

  async function save() {
    if (!profile) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await api.updateSender(profile.mailbox_id, {
        display_name: profile.display_name,
        title: profile.title,
        company: profile.company,
        positioning: profile.positioning,
        linkedin_url: profile.linkedin_url,
        phone: profile.phone,
        website: profile.website,
        signature: profile.signature,
        proof_points: profile.proof_points,
      });
      setProfile((prev) => (prev ? { ...prev, ...saved } : saved));
      setNotice("Saved. New drafts from this mailbox will use it.");
      loadList();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save");
    } finally {
      setSaving(false);
    }
  }

  async function upload(files: FileList | null) {
    if (!files || !profile) return;
    setError(null);
    setNotice(null);
    for (const file of Array.from(files)) {
      setUploading(file.name);
      try {
        const result = await api.uploadSenderDocument(profile.mailbox_id, file);
        setProfile(result.profile);
        const found = result.document.proof_point_count;
        setNotice(
          found > 0
            ? `${file.name}: ${found} proof point${found === 1 ? "" : "s"} added.`
            : `${file.name}: read, but nothing quotable was found in it.`
        );
      } catch (err) {
        setError(`${file.name}: ${err instanceof Error ? err.message : "upload failed"}`);
      } finally {
        setUploading(null);
      }
    }
    if (fileRef.current) fileRef.current.value = "";
    loadList();
  }

  async function removeDocument(documentId: string, filename: string) {
    if (!profile) return;
    if (!confirm(`Remove ${filename}? Its proof points will be dropped from the profile.`)) return;
    setBusy(true);
    try {
      const result = await api.deleteSenderDocument(profile.mailbox_id, documentId);
      setProfile((prev) => (prev ? { ...prev, ...result.profile } : prev));
      setNotice(`Removed ${filename}.`);
      loadList();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove that document");
    } finally {
      setBusy(false);
    }
  }

  async function reindex() {
    if (!profile) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.reindexSender(profile.mailbox_id);
      setProfile((prev) => (prev ? { ...prev, ...result.profile } : prev));
      setNotice(`Re-read ${result.reindexed} document(s).`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Re-index failed");
    } finally {
      setBusy(false);
    }
  }

  function removePoint(index: number) {
    setProfile((prev) =>
      prev ? { ...prev, proof_points: prev.proof_points.filter((_, i) => i !== index) } : prev
    );
  }

  function editPoint(index: number, text: string) {
    setProfile((prev) =>
      prev
        ? {
            ...prev,
            proof_points: prev.proof_points.map((p, i) =>
              i === index ? { ...p, text, pinned: true } : p
            ),
          }
        : prev
    );
  }

  function addPoint() {
    setProfile((prev) =>
      prev
        ? {
            ...prev,
            proof_points: [
              ...prev.proof_points,
              { text: "", source: "typed by hand", pinned: true } as ProofPoint,
            ],
          }
        : prev
    );
  }

  return (
    <main className="container">
      <Nav />
      <h1>Your profile</h1>
      <p className="lede">
        What each mailbox can credibly claim. Drafts open on the recipient and then have to earn
        the ask &mdash; this is what they earn it with. Upload a r&eacute;sum&eacute;, bio or deal
        sheet and the proof points are pulled out for you.
      </p>

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner success">{notice}</div>}
      {loading && <div className="banner">Loading&hellip;</div>}

      <div className="sender-layout">
        <div className="sender-list">
          {profiles.map((p) => (
            <button
              key={p.mailbox_id}
              className={`sender-pick${p.mailbox_id === selected ? " selected" : ""}`}
              onClick={() => setSelected(p.mailbox_id)}
            >
              <strong>{p.label || p.mailbox_id}</strong>
              <span className="meta">{p.from_email}</span>
              <span className="meta">
                {p.is_configured
                  ? `${p.proof_points.length} proof point(s) · ${p.document_count ?? 0} document(s)`
                  : "Not set up yet"}
              </span>
            </button>
          ))}
        </div>

        {profile && (
          <div className="sender-detail">
            <section className="card">
              <h2>{profile.label || profile.mailbox_id}</h2>
              <p className="meta">Outreach from this mailbox is signed as the person below.</p>

              <div className="field-grid">
                <label>
                  Name
                  <input
                    value={profile.display_name || ""}
                    onChange={(e) => patch("display_name", e.target.value)}
                    placeholder={profile.from_name || "Dalbir Bains"}
                  />
                </label>
                <label>
                  Title
                  <input
                    value={profile.title || ""}
                    onChange={(e) => patch("title", e.target.value)}
                    placeholder="CEO"
                  />
                </label>
                <label>
                  Company
                  <input
                    value={profile.company || ""}
                    onChange={(e) => patch("company", e.target.value)}
                    placeholder="Galaxy Pharma"
                  />
                </label>
                <label>
                  Phone
                  <input
                    value={profile.phone || ""}
                    onChange={(e) => patch("phone", e.target.value)}
                    placeholder="+1 646 957 7762"
                  />
                </label>
                <label>
                  LinkedIn
                  <input
                    value={profile.linkedin_url || ""}
                    onChange={(e) => patch("linkedin_url", e.target.value)}
                    placeholder="linkedin.com/in/dalbir-bains"
                  />
                </label>
                <label>
                  Website
                  <input
                    value={profile.website || ""}
                    onChange={(e) => patch("website", e.target.value)}
                    placeholder="galaxypharma.net"
                  />
                </label>
              </div>

              <label className="stacked">
                What you are doing now
                <textarea
                  rows={2}
                  value={profile.positioning || ""}
                  onChange={(e) => patch("positioning", e.target.value)}
                  placeholder="Leading World Reach Pharma, Keystone Capital's 503B compounding pharmacy platform."
                />
                <span className="meta">
                  One or two sentences. This is what the pitch hangs off.
                </span>
              </label>

              <label className="stacked">
                Signature
                <textarea
                  rows={4}
                  value={profile.signature || ""}
                  onChange={(e) => patch("signature", e.target.value)}
                  placeholder={profile.effective_signature || "Dalbir Bains\nCEO, Galaxy Pharma"}
                />
                <span className="meta">
                  Appended to every draft from this mailbox. Leave blank to use the fields above.
                </span>
              </label>

              {profile.effective_signature && !profile.signature && (
                <pre className="signature-preview">{profile.effective_signature}</pre>
              )}

              <div className="actions">
                <button className="primary" onClick={save} disabled={saving}>
                  {saving ? "Saving…" : "Save profile"}
                </button>
              </div>
            </section>

            <section className="card">
              <div className="card-head">
                <h2>Proof points</h2>
                <span className="meta">{profile.proof_points.length} in use</span>
              </div>
              <div className="explainer">
                <p>
                  <strong>A proof point is one specific thing you have actually done</strong>,
                  written so it can be dropped straight into an email &mdash; &ldquo;Raised and
                  deployed over $120M in capital and executed 50 acquisitions&rdquo;, not
                  &ldquo;strong leadership skills&rdquo;.
                </p>
                <p>
                  They are pulled out of the documents you upload below, and each one keeps the
                  sentence it came from, so nothing can be exaggerated &mdash; if your
                  r&eacute;sum&eacute; says $120M, no email can say $150M.
                </p>
                <p>
                  Every draft opens on the recipient, then uses <strong>at most one</strong> of
                  these to earn the ask, picking whichever fits that person. Edit or delete any
                  of them; anything you type by hand is kept when documents are re-read.
                </p>
              </div>

              <ul className="proof-list">
                {profile.proof_points.map((point, i) => (
                  <li key={i}>
                    <textarea
                      rows={2}
                      value={point.text}
                      onChange={(e) => editPoint(i, e.target.value)}
                    />
                    <div className="proof-foot">
                      <span className="meta">
                        {point.pinned ? "typed by hand" : `from ${point.source || "a document"}`}
                      </span>
                      <button className="link-btn danger" onClick={() => removePoint(i)}>
                        Remove
                      </button>
                    </div>
                    {point.quote && !point.pinned && (
                      <blockquote className="proof-quote">&ldquo;{point.quote}&rdquo;</blockquote>
                    )}
                  </li>
                ))}
              </ul>
              {profile.proof_points.length === 0 && (
                <p className="meta">
                  None yet. Upload a r&eacute;sum&eacute; below, or add one by hand.
                </p>
              )}

              <div className="actions">
                <button onClick={addPoint}>Add one by hand</button>
                <button className="primary" onClick={save} disabled={saving}>
                  {saving ? "Saving…" : "Save proof points"}
                </button>
              </div>

              {profile.keywords.length > 0 && (
                <div className="keyword-row">
                  {profile.keywords.map((k) => (
                    <span className="keyword" key={k}>
                      {k}
                    </span>
                  ))}
                </div>
              )}
            </section>

            <section className="card">
              <div className="card-head">
                <h2>Documents</h2>
                <button className="link-btn" onClick={reindex} disabled={busy}>
                  Re-read all
                </button>
              </div>
              <p className="meta">
                R&eacute;sum&eacute;s, bios, deal sheets, case studies. Accepted:{" "}
                {extensions.join(", ")}.
              </p>

              <input
                ref={fileRef}
                type="file"
                multiple
                accept={extensions.join(",")}
                onChange={(e) => upload(e.target.files)}
                disabled={!!uploading}
              />
              {uploading && <div className="banner">Reading {uploading}&hellip;</div>}

              <ul className="doc-list">
                {(profile.documents || []).map((doc) => (
                  <li key={doc.id}>
                    <div className="doc-head">
                      <strong>{doc.filename}</strong>
                      <span className={`doc-tag ${doc.kind}`}>{doc.kind.replace("_", " ")}</span>
                      <span className="meta">
                        {doc.proof_point_count} pt{doc.proof_point_count === 1 ? "" : "s"}
                      </span>
                      <button
                        className="link-btn danger"
                        onClick={() => removeDocument(doc.id, doc.filename)}
                        disabled={busy}
                      >
                        Delete
                      </button>
                    </div>
                    {doc.summary && <span className="meta">{doc.summary}</span>}
                    {doc.status !== "ready" && doc.error_message && (
                      <span className="meta warn">{doc.error_message}</span>
                    )}
                  </li>
                ))}
              </ul>
              {(profile.documents || []).length === 0 && (
                <p className="meta">Nothing uploaded yet.</p>
              )}
            </section>
          </div>
        )}
      </div>
    </main>
  );
}
