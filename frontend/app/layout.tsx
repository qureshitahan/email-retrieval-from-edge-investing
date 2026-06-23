import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Relationship Intelligence CRM",
  description: "Outlook Sent Items contact intelligence for Edge Investing",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body suppressHydrationWarning>{children}</body>
    </html>
  );
}
