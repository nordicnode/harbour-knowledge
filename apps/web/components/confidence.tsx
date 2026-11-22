import { cn } from "@/lib/cn";
import { confidenceLabel, confidenceSentence } from "@/lib/format";
import type { ConfidenceBand } from "@/lib/types";

export function ConfidenceMeter({
  band,
  mode,
  compact = false,
}: {
  band: ConfidenceBand | string | null | undefined;
  mode?: string;
  compact?: boolean;
}) {
  const level = band === "high" ? 3 : band === "medium" ? 2 : 1;
  const tone =
    band === "high" ? "bg-pine" : band === "medium" ? "bg-amber" : "bg-rust";

  if (compact) {
    return (
      <span className="inline-flex items-center gap-2 text-[11px] text-muted">
        <span className="flex items-end gap-[2px]">
          {[1, 2, 3].map((bar) => (
            <span
              key={bar}
              className={cn("w-[3px] rounded-sm", bar <= level ? tone : "bg-line-strong")}
              style={{ height: `${4 + bar * 2}px` }}
            />
          ))}
        </span>
        {confidenceLabel(band)}
      </span>
    );
  }

  return (
    <div className="flex items-center gap-3">
      <span className="flex items-end gap-[3px]">
        {[1, 2, 3].map((bar) => (
          <span
            key={bar}
            className={cn("w-[4px] rounded-sm", bar <= level ? tone : "bg-line-strong/70")}
            style={{ height: `${5 + bar * 3}px` }}
          />
        ))}
      </span>
      <div>
        <p className="text-[11.5px] font-medium text-ink">{confidenceLabel(band)}</p>
        <p className="text-[10.5px] text-faint">{confidenceSentence(band, mode)}</p>
      </div>
    </div>
  );
}
