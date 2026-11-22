"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";

export function SourceProgress({ active }: { active: boolean }) {
  const router = useRouter();

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => router.refresh(), 2500);
    return () => window.clearInterval(timer);
  }, [active, router]);

  if (!active) return null;

  return (
    <span className="inline-flex items-center gap-1.5 text-[10.5px] text-sky/80">
      <Loader2 size={11} className="animate-spin" /> updating automatically
    </span>
  );
}
