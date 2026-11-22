import { ButtonLink } from "@/components/ui";

export default function NotFound() {
  return (
    <div className="flex min-h-[50vh] flex-col items-center justify-center text-center">
      <p className="text-[10.5px] font-semibold uppercase tracking-[0.18em] text-faint">404</p>
      <h1 className="display mt-3 text-[32px] text-ink">Nothing lives here.</h1>
      <p className="mt-2 max-w-sm text-[13.5px] text-muted">
        The page may have moved, or the answer was archived.
      </p>
      <ButtonLink href="/" variant="primary" className="mt-6">
        Back to overview
      </ButtonLink>
    </div>
  );
}
