import type { Metadata } from "next";

import "./globals.css";
import { Sidebar } from "@/components/sidebar";
import { Topbar } from "@/components/topbar";
import { apiGet } from "@/lib/api";
import type { Meta } from "@/lib/types";

export const metadata: Metadata = {
  title: "Harbour Knowledge",
  description: "A governed support knowledge base.",
};

// Every workspace surface reads live governance state; none of it is static.
export const dynamic = "force-dynamic";

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  let meta: Meta | null = null;
  try {
    meta = await apiGet<Meta>("/v1/meta");
  } catch {
    meta = null;
  }

  return (
    <html lang="en">
      <body>
        <div className="flex min-h-screen">
          <Sidebar meta={meta} />
          <div className="min-w-0 flex-1">
            <Topbar meta={meta} />
            <main className="mx-auto w-full max-w-[1320px] px-5 py-7 md:px-8 md:py-9">{children}</main>
          </div>
        </div>
      </body>
    </html>
  );
}
