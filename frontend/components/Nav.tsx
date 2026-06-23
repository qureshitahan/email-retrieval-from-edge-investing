import Link from "next/link";

export function Nav() {
  return (
    <nav className="nav">
      <Link href="/">Contacts</Link>
      <Link href="/outreach">Outreach</Link>
    </nav>
  );
}
