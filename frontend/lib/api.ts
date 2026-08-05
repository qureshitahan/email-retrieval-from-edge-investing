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
  custom_instructions: string | null;
  system_prompt: string | null;
  user_prompt: string | null;
  error_message: string | null;
  sent_at: string | null;
  created_at: string;
  updated_at: string;
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
    mailboxIds: string[] = []
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
  generateDrafts: (contactIds: string[], customInstructions?: string, objective?: string) =>
    apiFetch<{ items: EmailDraft[]; results?: Array<{ contact_id: string; status: string; error?: string }> }>(
      "/outreach/drafts/generate",
      {
        method: "POST",
        body: JSON.stringify({
          contact_ids: contactIds,
          custom_instructions: customInstructions || null,
          objective: objective || null,
        }),
      }
    ),
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
