"use client";
import type { ReactNode } from "react";
import { SSEProvider } from "@/lib/sse";

export default function Providers({ children }: { children: ReactNode }) {
  return <SSEProvider>{children}</SSEProvider>;
}
