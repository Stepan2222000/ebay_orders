"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { fetchBadge } from "@/lib/truth";
import styles from "./truth.module.css";

const TABS = [
  { href: "/truth", label: "Разбор" },
  { href: "/truth/numbers", label: "Номера" },
  { href: "/truth/rules", label: "Правила" },
];

export default function TruthLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [open, setOpen] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const b = await fetchBadge();
        if (alive) setOpen(b.open);
      } catch { /* сервер недоступен — бейдж просто молчит */ }
    };
    tick();
    const t = setInterval(tick, 15000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <span className={styles.wordmark}>Истина</span>
        <nav className={styles.tabs}>
          {TABS.map((t) => {
            const active = t.href === "/truth"
              ? pathname === "/truth" || pathname.startsWith("/truth/listing")
              : pathname.startsWith(t.href);
            return (
              <Link key={t.href} href={t.href}
                className={`${styles.tab} ${active ? styles.tabActive : ""}`}>
                {t.label}
                {t.href === "/truth" && open !== null && (
                  <span className={`${styles.badge} ${open === 0 ? styles.badgeZero : ""}`}>
                    {open}
                  </span>
                )}
              </Link>
            );
          })}
        </nav>
        <div className={styles.spacer} />
        <Link href="/" className={styles.backlink}>← Заказы</Link>
      </header>
      <div className={styles.content}>{children}</div>
    </div>
  );
}
