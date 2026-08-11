import Link from "next/link";

export function Nav() {
  return (
    <nav className="nav">
      {/* Compass leads: the objective is the starting point, not the contact list. */}
      <Link href="/compass">Compass</Link>
      <Link href="/">Contacts</Link>
      <Link href="/outreach">Outreach</Link>
      <Link href="/senders">Your profile</Link>
    </nav>
  );
}
