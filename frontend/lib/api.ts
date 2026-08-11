const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000/api/v1";

export type Contact = {
  id: string;
  list_number: number | null;
  full_name: string | null;
  primary_email: string;
  company_name: string | null;
  company_domain: string | null;
  first_contacted_at: string | null;
  last_contacted_at: string | null;
  email_count: number;
  thread_count: number;
  fundraising_relevance_score: number;
  fundraising_relevance_tier: string | null;
  contact_type: string | null;
  status: string;
  review_status: string;
  notes: string | null;
  awaiting_reply: boolean;
  days_since_outreach: number | null;
  last_inbound_at: string | null;
  auto_context_short: string | null;
  detected_topics: string[] | null;
  last_subject: string | null;
  last_preview: string | null;
  latest_outlook_weblink: string | null;
  latest_message_id: string | null;
  has_ai_summary: boolean;
};

export type ContactDetail = Contact & {
  score_breakdown: Record<string, number> | null;
  auto_context_detailed: string | null;
  last_meaningful_email_preview: string | null;
  meaningful_previews: string[] | null;
  ai_summary: string | null;
  ai_follow_up_draft: string | null;
  ai_contact_classification: { contact_type: string; confidence: string; reason: string } | null;
  ai_summary_generated_at: string | null;
};

export type AIResult = {
  summary?: string;
  draft?: string;
  classification?: { contact_type: string; confidence: string; reason: string };
  cached: boolean;
  generated_at?: string | null;
};

export type Stats = {
  total_contacts: number;
  external_contacts: number;
  high_relevance_contacts: number;
  total_messages: number;
  synced_messages: number;
  graph_sent_total: number | null;
  sync_complete: boolean | null;
  unattributed_messages: number;
  review_pending: number;
  review_approved: number;
  review_denied: number;
  last_sync_at: string | null;
};

export type SyncRun = {
  id: string;
  sync_type: string;
  status: string;
  mailbox_id: string | null;
  messages_fetched: number;
  messages_new: number;
  contacts_updated: number;
  error_message: string | null;
  started_at: string;
  completed_at: string | null;
};

/** One thing the recipient has been doing, quoted from their own mail. */
export type ActivityNugget = {
  headline: string | null;
  detail: string | null;
  quote: string | null;
  date: string | null;
  said_by: "them" | "us" | null;
  is_recent: boolean;
  source_subject: string | null;
};

/** One question worth answering before searching, with the model's proposed answer. */
export type PlanQuestion = { question: string; answer: string; why?: string };

/** What the user actually meant by their objective, used to score the shortlist. */
export type ObjectivePlan = {
  objective: string;
  questions: PlanQuestion[];
  looking_for: string;
  avoid: string;
};

/** The evidence behind a draft's opening line, shown before it is sent. */
export type Personalization = {
  /** News worth congratulating them on. Often empty, and that is fine. */
  activity: ActivityNugget[];
  /** Everything else the mail establishes: what they offered, asked, are working on. */
  about_them: ActivityNugget[];
  focus: string[];
  note: string;
  studied_messages: number;
  full_bodies_read: number;
  reason: string;
  /** The objective this draft was written for. */
  objective?: string;
  /** Why the ranker chose this person for that objective. */
  selection_reason?: string;
  selection_score?: number | null;
};

export type EmailDraft = {
  id: string;
  contact_id: string;
  contact_name: string | null;
  contact_email: string | null;
  list_number: number | null;
  subject: string | null;
  body: string | null;
  status: string;
  sending_mailbox_id: string | null;
  personalization: Personalization | null;
  custom_instructions: string | null;
  system_prompt: string | null;
  user_prompt: string | null;
  error_message: string | null;
  sent_at: string | null;
  created_at: string;
  updated_at: string;
};

/** One claim the sender can make, quoted from a document they uploaded. */
export type ProofPoint = {
  text: string;
  quote?: string;
  source?: string;
  pinned?: boolean;
};

export type SenderDocument = {
  id: string;
  mailbox_id: string;
  filename: string;
  kind: string;
  char_count: number;
  proof_point_count: number;
  proof_points: ProofPoint[];
  keywords: string[];
  summary: string | null;
  status: "ready" | "text_only" | "failed";
  error_message: string | null;
  uploaded_at: string;
};

/** The sender's own story for one mailbox — what the pitch half of a draft is built from. */
export type SenderProfile = {
  mailbox_id: string;
  label?: string;
  from_email?: string;
  from_name?: string | null;
  display_name: string | null;
  title: string | null;
  company: string | null;
  positioning: string | null;
  linkedin_url: string | null;
  phone: string | null;
  website: string | null;
  signature: string | null;
  proof_points: ProofPoint[];
  keywords: string[];
  documents?: SenderDocument[];
  document_count?: number;
  effective_signature?: string;
  is_configured: boolean;
  updated_at: string | null;
};

/** One person's place in a drafting run — written, in hand, or still queued. */
export type DraftRunPerson = {
  contact_id: string;
  name: string;
  status: "done" | "writing" | "pending" | "failed" | "skipped";
  draft_id: string | null;
  error: string | null;
};

/** Progress of a background drafting run. */
export type DraftRun = {
  id: string;
  status: "running" | "completed" | "failed";
  phase: "studying" | "writing" | "done";
  total: number;
  completed: number;
  failed: number;
  done: number;
  percent: number;
  current_label: string | null;
  objective: string | null;
  draft_ids: string[];
  errors: Array<{ contact_id: string; error: string }>;
  error_message: string | null;
  started_at: string;
  updated_at: string;
  completed_at: string | null;
  /** Everyone in the batch and where each one has got to. */
  people?: DraftRunPerson[];
  /** Present on poll responses: the drafts finished so far. */
  items?: EmailDraft[];
  /** Present on start responses: true when attaching to a run already in flight. */
  already_running?: boolean;
};

export type Mailbox = {
  id: string;
  label: string;
  provider: string;
  from_email: string;
  from_name: string | null;
  can_send: boolean;
  auth_hint: string;
};

/** Live sync state for one mailbox, used for the progress bars. */
export type SyncProgress = {
  mailbox_id: string;
  from_email: string;
  label: string;
  state: "idle" | "running" | "completed" | "failed";
  is_running: boolean;
  synced_messages: number;
  /** null when the provider gives no folder total (Gmail) — show progress without a %. */
  remote_total: number | null;
  percent: number | null;
  contacts: number;
  sync_type: string | null;
  fetched_this_run: number;
  new_this_run: number;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
};

/** A contact scored against an objective. `objective_score` is null when unscored. */
export type RankedContact = {
  contact_id: string;
  list_number: number | null;
  full_name: string | null;
  primary_email: string;
  company_name: string | null;
  review_status: string;
  baseline_score: number;
  objective_score: number | null;
  reason: string | null;
  /** Exactly what the ranker was shown about this person, so a pick can be checked. */
  evidence: string | null;
};

/** A mailbox plus live readiness — `connected` means usable with no user action. */
export type MailboxStatus = Mailbox & {
  status: "ready" | "needs_signin" | "needs_consent" | "not_configured" | "error";
  can_read: boolean;
  connected: boolean;
  needs_signin: boolean;
  requires_interactive_auth: boolean;
  detail: string;
};

export type OutreachPrompt = {
  system_prompt: string;
  user_prompt_template: string;
  updated_at: string;
};

export type AuthStatus = {
  connected: boolean;
  user_email: string | null;
  can_send_mail?: boolean;
  token_scopes?: string[];
};

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    try {
      const json = JSON.parse(text);
      throw new Error(json.detail || text || res.statusText);
    } catch {
      throw new Error(text || res.statusText);
    }
  }
  return res.json();
}

export const api = {
  authStatus: () => apiFetch<AuthStatus>("/auth/status"),
  stats: (mailboxId?: string) =>
    apiFetch<Stats>(
      `/contacts/stats${mailboxId ? `?mailbox_id=${encodeURIComponent(mailboxId)}` : ""}`
    ),
  contacts: (params: Record<string, string | number | boolean>) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== "" && v !== undefined && v !== null) qs.set(k, String(v));
    });
    return apiFetch<{ items: Contact[]; total: number; page: number; page_size: number }>(
      `/contacts?${qs.toString()}`
    );
  },
  contact: (id: string) => apiFetch<ContactDetail>(`/contacts/${id}`),
  updateContact: (id: string, data: { review_status?: string; notes?: string }) =>
    apiFetch<ContactDetail>(`/contacts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  contactMessages: (id: string) =>
    apiFetch<
      Array<{
        id: string;
        subject: string;
        sent_datetime: string;
        body_preview: string;
        outlook_weblink: string;
        has_attachments: boolean;
      }>
    >(`/contacts/${id}/messages`),
  startSync: (mailboxId?: string) =>
    apiFetch<SyncRun>(`/sync/start${mailboxId ? `?mailbox_id=${encodeURIComponent(mailboxId)}` : ""}`, {
      method: "POST",
    }),
  startInboxSync: (mailboxId?: string) =>
    apiFetch<SyncRun>(
      `/sync/start-inbox${mailboxId ? `?mailbox_id=${encodeURIComponent(mailboxId)}` : ""}`,
      { method: "POST" }
    ),
  syncProgress: () => apiFetch<{ items: SyncProgress[]; any_running: boolean }>("/sync/progress"),
  syncStatus: (mailboxId?: string) =>
    apiFetch<SyncRun | null>(
      `/sync/status${mailboxId ? `?mailbox_id=${encodeURIComponent(mailboxId)}` : ""}`
    ),
  backfillMailbox: (mailboxId: string) =>
    apiFetch<{
      mailbox_id: string;
      from_email: string;
      messages_updated: number;
      sync_runs_updated: number;
      messages_still_unattributed: number;
    }>("/sync/backfill-mailbox", {
      method: "POST",
      body: JSON.stringify({ mailbox_id: mailboxId }),
    }),
  loginUrl: () => `${API_BASE}/auth/login`,
  getOutreachPrompt: () => apiFetch<OutreachPrompt>("/outreach/prompt"),
  saveOutreachPrompt: (data: { system_prompt?: string; user_prompt_template?: string }) =>
    apiFetch<OutreachPrompt>("/outreach/prompt", { method: "PATCH", body: JSON.stringify(data) }),
  listDrafts: (status?: string) =>
    apiFetch<{ items: EmailDraft[] }>(`/outreach/drafts${status ? `?status=${status}` : ""}`),
  mailboxes: () => apiFetch<{ items: Mailbox[] }>("/outreach/mailboxes"),
  prioritize: (
    objective: string,
    contactIds: string[] = [],
    limit = 200,
    topN: number | null = 50,
    mailboxIds: string[] = [],
    plan: ObjectivePlan | null = null
  ) =>
    apiFetch<{
      objective: string;
      scored: number;
      scanned: number;
      batches: number;
      failed_batches: number;
      items: RankedContact[];
    }>("/outreach/prioritize", {
      method: "POST",
      body: JSON.stringify({
        objective,
        contact_ids: contactIds,
        limit,
        top_n: topN,
        mailbox_ids: mailboxIds,
        plan,
      }),
    }),
  bulkReview: (contactIds: string[], reviewStatus: "approved" | "denied" | "pending") =>
    apiFetch<{ review_status: string; requested: number; updated: number; not_found: number }>(
      "/contacts/bulk-review",
      {
        method: "POST",
        body: JSON.stringify({ contact_ids: contactIds, review_status: reviewStatus }),
      }
    ),
  /** Mailboxes with live readiness — drives the mailbox dropdown on both pages. */
  mailboxStatuses: (refresh = false) =>
    apiFetch<{ items: MailboxStatus[]; config_error: string | null; sendable?: string[] }>(
      `/mailboxes?refresh=${refresh}`
    ),
  sendDraftBatch: (draftIds: string[], mailboxId?: string) =>
    apiFetch<{
      results: Array<{
        draft_id: string;
        status: string;
        error?: string;
        mailbox_id?: string;
        to?: string;
      }>;
      sent: number;
      failed: number;
      by_mailbox: Record<string, number>;
    }>("/outreach/drafts/send-batch", {
      method: "POST",
      body: JSON.stringify({ draft_ids: draftIds, mailbox_id: mailboxId ?? null }),
    }),
  generateDrafts: (
    contactIds: string[],
    customInstructions?: string,
    objective?: string,
    mailboxIds: string[] = []
  ) =>
    apiFetch<{ items: EmailDraft[]; results?: Array<{ contact_id: string; status: string; error?: string }> }>(
      "/outreach/drafts/generate",
      {
        method: "POST",
        body: JSON.stringify({
          contact_ids: contactIds,
          custom_instructions: customInstructions || null,
          objective: objective || null,
          mailbox_ids: mailboxIds,
        }),
      }
    ),
  /**
   * Queue a bulk drafting run and return at once.
   *
   * The synchronous `generateDrafts` above is kept for one-off use, but a batch must not be
   * written inside a single request: Azure closes the connection after 230 seconds and the
   * browser reports "Failed to fetch" while the server is still working. Poll `draftRun`.
   */
  startDrafting: (
    contactIds: string[],
    customInstructions?: string,
    objective?: string,
    mailboxIds: string[] = [],
    reasons: Array<{ contact_id: string; reason: string | null; score: number | null }> = []
  ) =>
    apiFetch<DraftRun>("/outreach/drafts/start", {
      method: "POST",
      body: JSON.stringify({
        contact_ids: contactIds,
        custom_instructions: customInstructions || null,
        objective: objective || null,
        mailbox_ids: mailboxIds,
        reasons,
      }),
    }),
  objectivePlan: (objective: string) =>
    apiFetch<ObjectivePlan>("/outreach/objective/plan", {
      method: "POST",
      body: JSON.stringify({ objective }),
    }),
  senders: () =>
    apiFetch<{ items: SenderProfile[]; supported_extensions: string[] }>("/senders"),
  sender: (mailboxId: string) =>
    apiFetch<SenderProfile & { supported_extensions: string[] }>(
      `/senders/${encodeURIComponent(mailboxId)}`
    ),
  updateSender: (mailboxId: string, patch: Partial<SenderProfile>) =>
    apiFetch<SenderProfile>(`/senders/${encodeURIComponent(mailboxId)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  /**
   * Upload a résumé, bio or deal sheet.
   *
   * Deliberately not routed through `apiFetch`: that sets a JSON content type, and a multipart
   * body needs the browser to set its own boundary header.
   */
  uploadSenderDocument: async (mailboxId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${API_BASE}/senders/${encodeURIComponent(mailboxId)}/documents`, {
      method: "POST",
      body: form,
      cache: "no-store",
    });
    const text = await res.text();
    if (!res.ok) {
      try {
        throw new Error(JSON.parse(text).detail || text || res.statusText);
      } catch (err) {
        throw err instanceof Error ? err : new Error(text || res.statusText);
      }
    }
    return JSON.parse(text) as {
      document: SenderDocument;
      profile: SenderProfile;
      truncated: boolean;
    };
  },
  deleteSenderDocument: (mailboxId: string, documentId: string) =>
    apiFetch<{ deleted: string; profile: SenderProfile }>(
      `/senders/${encodeURIComponent(mailboxId)}/documents/${documentId}`,
      { method: "DELETE" }
    ),
  reindexSender: (mailboxId: string) =>
    apiFetch<{ reindexed: number; profile: SenderProfile }>(
      `/senders/${encodeURIComponent(mailboxId)}/reindex`,
      { method: "POST" }
    ),
  draftRun: (runId: string) => apiFetch<DraftRun>(`/outreach/drafts/runs/${runId}`),
  latestDraftRun: () => apiFetch<DraftRun | null>("/outreach/drafts/runs/latest"),
  generateDraftForContact: (contactId: string, customInstructions?: string, objective?: string) =>
    apiFetch<EmailDraft>(`/outreach/contacts/${contactId}/generate`, {
      method: "POST",
      body: JSON.stringify({
        custom_instructions: customInstructions || null,
        objective: objective || null,
      }),
    }),
  updateDraft: (id: string, data: { subject?: string; body?: string; status?: string }) =>
    apiFetch<EmailDraft>(`/outreach/drafts/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  approveDraft: (id: string) => apiFetch<EmailDraft>(`/outreach/drafts/${id}/approve`, { method: "POST" }),
  /** Discard a draft. There is no delete route, so it is marked and filtered out of views. */
  discardDraft: (id: string) =>
    apiFetch<EmailDraft>(`/outreach/drafts/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status: "discarded" }),
    }),
  setDraftMailbox: (id: string, mailboxId: string) =>
    apiFetch<EmailDraft>(`/outreach/drafts/${id}/sending-mailbox`, {
      method: "POST",
      body: JSON.stringify({ mailbox_id: mailboxId }),
    }),
  sendDraft: (id: string, mailboxId?: string) =>
    apiFetch<EmailDraft>(`/outreach/drafts/${id}/send`, {
      method: "POST",
      body: JSON.stringify({ mailbox_id: mailboxId || null }),
    }),
  sendApprovedDrafts: (mailboxId?: string) =>
    apiFetch<{ results: Array<{ draft_id: string; status: string; error?: string }> }>(
      "/outreach/drafts/send-approved",
      { method: "POST", body: JSON.stringify({ mailbox_id: mailboxId || null }) }
    ),
  exportXlsxUrl: () => `${API_BASE}/export/contacts.xlsx`,
  exportCsvUrl: () => `${API_BASE}/export/contacts.csv`,
  openOutlookUrl: (messageId: string) => `${API_BASE}/messages/${messageId}/open-outlook`,
  aiStatus: (id: string) =>
    apiFetch<{
      has_summary: boolean;
      has_follow_up: boolean;
      has_classification: boolean;
      summary_generated_at: string | null;
      needs_refresh: boolean;
    }>(`/contacts/${id}/ai/status`),
  aiSummary: (id: string, force = false) =>
    apiFetch<AIResult>(`/contacts/${id}/ai/summary?force=${force}`, { method: "POST" }),
  aiFollowUp: (id: string, force = false) =>
    apiFetch<AIResult>(`/contacts/${id}/ai/follow-up?force=${force}`, { method: "POST" }),
  aiClassify: (id: string, force = false) =>
    apiFetch<AIResult>(`/contacts/${id}/ai/classify?force=${force}`, { method: "POST" }),
  aiSummarizeThreads: (id: string, force = false) =>
    apiFetch<AIResult>(`/contacts/${id}/ai/summarize-threads?force=${force}`, { method: "POST" }),
  aiRelationship: (id: string, force = false, objective?: string) => {
    const qs = new URLSearchParams({ force: String(force) });
    if (objective) qs.set("objective", objective);
    return apiFetch<{ relationship_context: string; cached: boolean; generated_at?: string | null }>(
      `/contacts/${id}/ai/relationship?${qs.toString()}`,
      { method: "POST" }
    );
  },
};

export function formatDate(value: string | null) {
  if (!value) return "—";
  return new Date(value).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function tierClass(tier: string | null) {
  if (tier === "high") return "tier high";
  if (tier === "medium") return "tier medium";
  return "tier low";
}

export function reviewClass(status: string | null) {
  if (status === "approved") return "review approved";
  if (status === "denied") return "review denied";
  return "review pending";
}
