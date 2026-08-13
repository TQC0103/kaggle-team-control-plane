import vinext from "vinext";
import { defineConfig } from "vite";

// The local control-plane dashboard only needs the React/RSC application.
// Hosting bindings (Cloudflare, D1, R2) are intentionally omitted here: they
// add startup work and can contend on Wrangler state even though this dashboard
// talks directly to the loopback Python API.
export default defineConfig({
  plugins: [vinext()],
});
