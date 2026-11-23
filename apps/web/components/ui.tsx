import Link from "next/link";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { ArrowUpRight, Inbox } from "lucide-react";

import { cn } from "@/lib/cn";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "quiet";
type Size = "sm" | "md" | "lg";

const variants: Record<Variant, string> = {
  primary:
    "bg-pine text-[#0c1711] shadow-[0_1px_0_rgba(0,0,0,0.18)] hover:bg-pine-dark disabled:bg-pine/50 disabled:text-ink-2",
  secondary:
    "border border-line bg-surface text-ink hover:border-line-strong hover:bg-surface-2 disabled:opacity-50",
  ghost: "text-ink-2 hover:bg-paper-deep disabled:opacity-50",
  danger: "bg-rust text-[#1a0e0a] hover:bg-rust/90 disabled:opacity-50",
  quiet: "border border-line/70 bg-transparent text-ink-2 hover:bg-paper-deep",
};

const sizes: Record<Size, string> = {
  sm: "h-8 gap-1.5 px-3 text-[12.5px]",
  md: "h-9 gap-2 px-4 text-[13.5px]",
  lg: "h-10 gap-2 px-5 text-sm",
};

export function buttonStyles(variant: Variant = "secondary", size: Size = "md", className?: string) {
  return cn(
    "inline-flex shrink-0 items-center justify-center rounded-lg font-medium transition-colors disabled:cursor-not-allowed",
    variants[variant],
    sizes[size],
    className,
  );
}

export function Button({
  variant = "secondary",
  size = "md",
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: Size }) {
  return <button className={buttonStyles(variant, size, className)} {...props} />;
}

export function ButtonLink({
  href,
  variant = "secondary",
  size = "md",
  className,
  children,
  external,
}: {
  href: string;
  variant?: Variant;
  size?: Size;
  className?: string;
  children: ReactNode;
  external?: boolean;
}) {
  return (
    <Link href={href} className={buttonStyles(variant, size, className)} target={external ? "_blank" : undefined}>
      {children}
    </Link>
  );
}

export function Card({
  className,
  children,
  id,
  as: Tag = "div",
}: {
  className?: string;
  children: ReactNode;
  id?: string;
  as?: "div" | "section" | "article";
}) {
  return (
    <Tag
      id={id}
      className={cn(
        "rounded-[14px] border border-line bg-surface shadow-[0_1px_0_rgba(0,0,0,0.35)]",
        className,
      )}
    >
      {children}
    </Tag>
  );
}

const tones = {
  neutral: "bg-paper-deep text-ink-2 border-transparent",
  pine: "bg-pine-soft text-pine-dark border-transparent",
  amber: "bg-amber-soft text-amber border-transparent",
  rust: "bg-rust-soft text-rust border-transparent",
  sky: "bg-sky-soft text-sky border-transparent",
  outline: "bg-transparent text-muted border-line",
} as const;

export type Tone = keyof typeof tones;

export function Badge({
  tone = "neutral",
  children,
  className,
  dot,
}: {
  tone?: Tone;
  children: ReactNode;
  className?: string;
  dot?: boolean;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium tracking-[0.01em]",
        tones[tone],
        className,
      )}
    >
      {dot ? <span className="h-1.5 w-1.5 rounded-full bg-current opacity-70" /> : null}
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const map: Record<string, Tone> = {
    approved: "pine",
    active: "pine",
    succeeded: "pine",
    answered: "pine",
    in_review: "sky",
    pending: "amber",
    processing: "sky",
    running: "sky",
    queued: "sky",
    needs_recheck: "amber",
    draft: "neutral",
    archived: "neutral",
    withdrawn: "neutral",
    failed: "rust",
    quarantined: "rust",
    wont_answer: "neutral",
    open: "amber",
  };
  return (
    <Badge tone={map[status] ?? "neutral"} dot>
      {status.replace(/_/g, " ")}
    </Badge>
  );
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
  className,
}: {
  eyebrow?: string;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header className={cn("flex flex-col gap-5 border-b border-line pb-6 md:flex-row md:items-end md:justify-between", className)}>
      <div className="max-w-3xl">
        {eyebrow ? (
          <p className="mb-2 text-[10.5px] font-semibold uppercase tracking-[0.16em] text-muted">{eyebrow}</p>
        ) : null}
        <h1 className="display text-[32px] leading-[1.08] text-ink md:text-[36px]">{title}</h1>
        {description ? <p className="mt-2.5 max-w-2xl text-[14px] leading-relaxed text-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function SectionHeading({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-4 flex items-end justify-between gap-4">
      <div>
        <h2 className="display text-[19px] leading-tight text-ink">{title}</h2>
        {hint ? <p className="mt-1 text-[12.5px] text-muted">{hint}</p> : null}
      </div>
      {action}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
  icon,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-[14px] border border-dashed border-line-strong bg-surface/60 px-6 py-14 text-center">
      <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-full border border-line bg-paper text-muted">
        {icon ?? <Inbox size={19} />}
      </div>
      <p className="display text-[19px] text-ink">{title}</p>
      {description ? <p className="mt-1.5 max-w-sm text-[13px] text-muted">{description}</p> : null}
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}

export function Field({
  label,
  hint,
  children,
  className,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={cn("block", className)}>
      <span className="mb-1.5 block text-[11.5px] font-semibold uppercase tracking-[0.08em] text-muted">{label}</span>
      {children}
      {hint ? <span className="mt-1.5 block text-[11.5px] leading-snug text-faint">{hint}</span> : null}
    </label>
  );
}

export function FieldGroup({
  label,
  hint,
  children,
  className,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("block", className)}>
      <span className="mb-1.5 block text-[11.5px] font-semibold uppercase tracking-[0.08em] text-muted">{label}</span>
      {children}
      {hint ? <span className="mt-1.5 block text-[11.5px] leading-snug text-faint">{hint}</span> : null}
    </div>
  );
}

export const inputClass =
  "h-9 w-full rounded-lg border border-line bg-surface px-3 text-[13.5px] text-ink placeholder:text-faint focus:border-pine/50";
export const selectClass =
  "h-9 w-full appearance-none rounded-lg border border-line bg-surface px-3 pr-8 text-[13.5px] text-ink focus:border-pine/50";
export const textareaClass =
  "w-full rounded-lg border border-line bg-surface px-3 py-2.5 text-[13.5px] leading-relaxed text-ink placeholder:text-faint focus:border-pine/50";

export function ExternalLink({
  href,
  children,
}: {
  href: string;
  children: ReactNode;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 text-pine underline decoration-pine/25 underline-offset-2 hover:decoration-pine"
    >
      {children}
      <ArrowUpRight size={12} />
    </a>
  );
}
