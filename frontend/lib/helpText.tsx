import { ReactNode } from "react";

export function ScoreBreakdownHelp() {
  return (
    <div className="help-text">
      <p>
        <strong>Automatic ranking score</strong> — built from email metadata only (no AI). Used to sort
        who to review first, not to confirm someone is an investor.
      </p>
      <p className="help-subhead">Positive signals</p>
      <ul>
        <li>
          <strong>Investor domain (+30)</strong> — company/domain contains capital, ventures, fund, equity,
          partners, etc.
        </li>
        <li>
          <strong>Fundraising keywords (up to +20)</strong> — investor, raise, deck, funding, term sheet, CIM…
        </li>
        <li>
          <strong>Pharma keywords (up to +15)</strong> — pharma, healthcare, 503b, clinical…
        </li>
        <li>
          <strong>Multiple emails (+15)</strong> — 2 or more sent emails to this person
        </li>
        <li>
          <strong>Recent contact (+10)</strong> — last email within 12 months
        </li>
        <li>
          <strong>Attachments (+10)</strong> — at least one sent email had an attachment
        </li>
        <li>
          <strong>Engagement keywords (+10)</strong> — intro, call, meeting, follow-up in subject/preview
        </li>
      </ul>
      <p className="help-subhead">Negative adjustments</p>
      <ul>
        <li>
          <strong>Internal contact (−30)</strong> — edgeinvesting.ca / galaxypharma domains
        </li>
        <li>
          <strong>Personal email (−10)</strong> — Gmail, Yahoo, Hotmail, etc.
        </li>
        <li>
          <strong>Excluded / noise (−20)</strong> — filtered mass-mail or low-value contacts
        </li>
      </ul>
    </div>
  );
}

export function RelevanceTierHelp() {
  return (
    <div className="help-text">
      <p>
        <strong>Relevance tiers</strong> — from the total fundraising score. Helps you prioritize review,
        not prove investor status.
      </p>
      <ul>
        <li>
          <strong>Tier 1 — High</strong> — score 50+. Strong signals (investor domain, fundraising language,
          multiple touchpoints).
        </li>
        <li>
          <strong>Tier 2 — Medium</strong> — score 25–49. Some relevant signals worth a look.
        </li>
        <li>
          <strong>Tier 3 — Low</strong> — score below 25. Weaker or fewer signals.
        </li>
      </ul>
    </div>
  );
}
