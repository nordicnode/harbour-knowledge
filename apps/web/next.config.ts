import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  poweredByHeader: false,
  // The preview is served over 127.0.0.1 while Next binds localhost; allow it
  // so the dev client (HMR and dev fonts) is not blocked as cross-origin.
  allowedDevOrigins: ["127.0.0.1"],
};

export default nextConfig;
