import type { NextConfig } from "next";

// API base lives in NEXT_PUBLIC_API_BASE so the browser can call FastAPI
// directly (CORS-allowlisted there). No rewrites — turbopack in Next.js 16
// dev does not proxy external rewrites cleanly.
const nextConfig: NextConfig = {
  allowedDevOrigins: ["127.0.0.1"],
};

export default nextConfig;
