import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server binds localhost by default, which is a WebXR "secure context"
// when reached through `adb reverse tcp:5180 tcp:5180` from the Quest.
// Use `npm run host` to expose on the LAN instead (then you need HTTPS for WebXR).
export default defineConfig({
  plugins: [react()],
  server: {
    // 5180 to avoid clashing with the friend-lite webui dev server (5173).
    port: 5180,
    strictPort: true,
  },
});
