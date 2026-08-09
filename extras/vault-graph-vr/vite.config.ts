import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import fs from "node:fs";
import path from "node:path";

// Receives a PNG (base64 data URL) from the in-headset capture button and
// writes it to ./captures so it can be inspected outside the headset.
function captureEndpoint(): Plugin {
  return {
    name: "vault-graph-vr-capture",
    configureServer(server) {
      // Browser log/error relay → ./captures/debug.log + terminal.
      server.middlewares.use("/__log", (req, res) => {
        if (req.method !== "POST") {
          res.statusCode = 405;
          return res.end();
        }
        let body = "";
        req.on("data", (c) => (body += c));
        req.on("end", () => {
          try {
            const dir = path.resolve(process.cwd(), "captures");
            fs.mkdirSync(dir, { recursive: true });
            const line = `${new Date().toISOString()} ${body}\n`;
            fs.appendFileSync(path.join(dir, "debug.log"), line);
            server.config.logger.info(`[app] ${body}`);
          } catch {
            /* ignore */
          }
          res.end("ok");
        });
      });

      server.middlewares.use("/__capture", (req, res) => {
        if (req.method !== "POST") {
          res.statusCode = 405;
          return res.end();
        }
        let body = "";
        req.on("data", (c) => (body += c));
        req.on("end", () => {
          try {
            const { dataUrl } = JSON.parse(body);
            const b64 = String(dataUrl).replace(/^data:image\/png;base64,/, "");
            const dir = path.resolve(process.cwd(), "captures");
            fs.mkdirSync(dir, { recursive: true });
            const file = path.join(dir, `view-${Date.now()}.png`);
            fs.writeFileSync(file, Buffer.from(b64, "base64"));
            res.setHeader("content-type", "application/json");
            res.end(JSON.stringify({ ok: true, file }));
            server.config.logger.info(`[capture] wrote ${file}`);
          } catch (e) {
            res.statusCode = 500;
            res.end(String(e));
          }
        });
      });
    },
  };
}

// Dev server binds localhost by default, which is a WebXR "secure context"
// when reached through `adb reverse tcp:5180 tcp:5180` from the Quest.
// Use `npm run host` to expose on the LAN instead (then you need HTTPS for WebXR).
export default defineConfig({
  plugins: [react(), captureEndpoint()],
  server: {
    // 5180 to avoid clashing with the Chronicle webui dev server (5173).
    port: 5180,
    strictPort: true,
  },
});
