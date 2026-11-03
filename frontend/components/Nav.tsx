"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Documents" },
  { href: "/search", label: "Search" },
  { href: "/crosscheck", label: "Cross-check" },
];

export default function Nav() {
  const pathname = usePathname();
  return (
    <nav>
      {LINKS.map((link) => (
        <Link
          key={link.href}
          href={link.href}
          className={pathname === link.href ? "active" : ""}
        >
          {link.label}
        </Link>
      ))}
    </nav>
  );
}
