"use client";

import { ReactNode } from "react";

type InfoTipProps = {
  label?: string;
  children: ReactNode;
};

export function InfoTip({ label = "More information", children }: InfoTipProps) {
  return (
    <span className="info-tip">
      <button type="button" className="info-tip-trigger" aria-label={label} tabIndex={0}>
        ⓘ
      </button>
      <span className="info-tip-popover" role="tooltip">
        {children}
      </span>
    </span>
  );
}

export function SectionHeading({ title, tip }: { title: string; tip?: ReactNode }) {
  return (
    <h3 className="section-heading">
      {title}
      {tip}
    </h3>
  );
}
