"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { BookOpen, Inbox, Sparkles } from "lucide-react";

import { cn } from "@/lib/cn";
import type { Meta } from "@/lib/types";

type NavItem = {
  href: string;
  label: string;
  icon: typeof BookOpen;
  match?: string[];
  attention?: boolean;
};

const NAV: NavItem[] = [
  { href: "/knowledge", label: "Knowledge", icon: BookOpen, match: ["/knowledge", "/sources", "/topics", "/gaps"] },
  { href: "/ask", label: "Ask", icon: Sparkles },
  { href: "/review", label: "Review", icon: Inbox, attention: true },
];

function initials(name: string): string {
  return name
    .split(" ")
    .map((part) => part[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
}

export function Sidebar({ meta }: { meta: Meta | null }) {
  const pathname = usePathname();
  const member = meta?.member;
  const pendingReview = meta?.review?.pending ?? 0;
  const nav = NAV.filter((item) => !item.attention || pendingReview > 0);
  const localEmbeddings = meta?.ai.embedding_provider === "local";
  const providerLabel =
    meta?.ai.mode === "openai"
      ? "Provider connected"
      : localEmbeddings
        ? "Local embeddings"
        : "Local dev intelligence";

  return (
    <aside className="sticky top-0 hidden h-screen w-[232px] shrink-0 flex-col bg-[#131914] text-[#e9e4d8] lg:flex">
      <div className="px-5 pb-8 pt-6">
        <Link href="/knowledge" className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-[9px] bg-[#d8e4d8] text-[#16352a]">
            <span className="display text-[19px] font-semibold">H</span>
          </div>
          <div className="min-w-0">
            <p className="display text-[18px] leading-none text-[#f3efe5]">Harbour</p>
            <p className="mt-1 truncate text-[9px] font-medium uppercase tracking-[0.16em] text-[#98a294]">
              {meta?.workspace.name ?? "Support knowledge"}
            </p>
          </div>
        </Link>
      </div>

      <nav className="flex-1 px-3">
        <ul className="space-y-0.5">
          {nav.map((item) => {
            const match = item.match ?? [item.href];
            const active = match.some((href) => pathname === href || pathname.startsWith(`${href}/`));
            const Icon = item.icon;
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className={cn(
                    "group flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-[13px] transition-colors",
                    active
                      ? "bg-white/[0.085] text-[#f4f0e6]"
                      : "text-[#aeb6a8] hover:bg-white/[0.045] hover:text-[#e9e4d8]",
                  )}
                >
                  <Icon
                    size={16}
                    strokeWidth={1.7}
                    className={active ? "text-[#cfe3d4]" : "text-[#8a9587] group-hover:text-[#c9d2c5]"}
                  />
                  {item.label}
                  {item.attention && pendingReview > 0 ? (
                    <span
                      className="ml-auto rounded-full bg-[#d2a05d]/15 px-1.5 py-0.5 text-[10px] font-semibold text-[#e0b878]"
                      aria-label={`${pendingReview} pending decisions`}
                    >
                      {pendingReview}
                    </span>
                  ) : null}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      <div className="px-5 pb-4">
        <p className="flex items-center gap-1.5 text-[10px] text-[#7f8b7c]">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              meta?.ai.mode === "openai" ? "bg-[#8fc5a1]" : "bg-[#d2a05d]",
            )}
          />
          {providerLabel}
        </p>
      </div>

      <div className="border-t border-white/[0.07] px-5 py-4">
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-full bg-[#d9c9ae] text-[11px] font-semibold text-[#3b3020]">
            {member ? initials(member.name) : "MC"}
          </div>
          <div className="min-w-0 flex-1">
            <p className="truncate text-[12.5px] font-medium text-[#eee9de]">{member?.name ?? "Maya Chen"}</p>
            <p className="truncate text-[10.5px] text-[#818c7e]">{member?.title ?? "Head of Support"}</p>
          </div>
          <span className="rounded border border-white/[0.09] px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-[#aab3a5]">
            {member?.role ?? "admin"}
          </span>
        </div>
      </div>
    </aside>
  );
}
