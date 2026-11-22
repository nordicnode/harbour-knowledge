"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Plus, Search } from "lucide-react";

import { cn } from "@/lib/cn";
import type { Meta } from "@/lib/types";

const MOBILE_NAV = [
  { href: "/knowledge", label: "Knowledge" },
  { href: "/ask", label: "Ask" },
  { href: "/review", label: "Review" },
];

const TITLES: Record<string, string> = {
  "": "Knowledge",
  ask: "Ask",
  review: "Review",
  knowledge: "Knowledge",
  sources: "Knowledge",
  gaps: "Knowledge",
  topics: "Knowledge",
  search: "Search",
};

export function Topbar({ meta }: { meta: Meta | null }) {
  const pathname = usePathname();
  const segment = pathname.split("/")[1] ?? "";
  const title = TITLES[segment] ?? segment;
  const pendingReview = meta?.review?.pending ?? 0;
  const mobileNav = MOBILE_NAV.filter((item) => item.href !== "/review" || pendingReview > 0);

  return (
    <div className="sticky top-0 z-20 border-b border-line bg-paper/90 backdrop-blur-md">
      <div className="flex h-[56px] items-center justify-between gap-4 px-5 md:px-8">
        <div className="flex min-w-0 items-center gap-2 text-[12px]">
          <Link href="/knowledge" className="text-faint transition-colors hover:text-ink">
            {meta?.workspace.name ?? "Harbour Support"}
          </Link>
          <span className="text-line-strong">/</span>
          <span className="truncate font-medium text-ink-2">{title}</span>
        </div>

        <div className="flex items-center gap-3">
          <form action="/search" className="relative hidden md:block">
            <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
            <input
              name="q"
              placeholder="Search knowledge"
              aria-label="Search knowledge"
              className="h-8 w-[210px] rounded-lg border border-line bg-surface pl-8 pr-2.5 text-[12px] text-ink placeholder:text-faint focus:border-pine/50"
            />
          </form>
          <Link
            href="/search"
            aria-label="Search"
            className="flex h-8 w-8 items-center justify-center rounded-lg border border-line bg-surface text-ink-2 md:hidden"
          >
            <Search size={14} />
          </Link>
          <Link
            href="/knowledge#add"
            className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-pine px-3 text-[12px] font-medium text-white transition-colors hover:bg-pine-dark"
          >
            <Plus size={13} />
            Add knowledge
          </Link>
        </div>
      </div>
      <nav className="flex gap-1 overflow-x-auto border-t border-line px-3 py-2 lg:hidden">
        {mobileNav.map((item) => {
          const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "shrink-0 rounded-md px-2.5 py-1 text-[11.5px] transition-colors",
                active ? "bg-pine-soft font-medium text-pine-dark" : "text-muted hover:bg-paper-deep hover:text-ink",
              )}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
