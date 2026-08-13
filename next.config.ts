import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The dashboard has no server-rendered data or server actions. Exporting it
  // as static files removes the React server cold start from daily local use.
  output: "export",
};

export default nextConfig;
