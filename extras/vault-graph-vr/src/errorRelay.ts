// Relays browser logs/errors to the dev server so they can be read outside the
// headset. Critically, it wraps console.error so errors that are otherwise
// swallowed by `.catch(console.error)` (as @react-three/xr does for controller
// layout loading) become visible in ./captures/debug.log.

function stringify(args: unknown[]): string {
  return args
    .map((a) => {
      if (a instanceof Error) return a.stack || `${a.name}: ${a.message}`;
      if (typeof a === "object" && a !== null) {
        try {
          return JSON.stringify(a);
        } catch {
          return String(a);
        }
      }
      return String(a);
    })
    .join(" ");
}

function post(level: string, args: unknown[]) {
  try {
    fetch("/__log", {
      method: "POST",
      headers: { "content-type": "text/plain" },
      body: `[${level}] ${stringify(args)}`,
      keepalive: true,
    }).catch(() => {});
  } catch {
    /* ignore */
  }
}

let installed = false;

export function installErrorRelay() {
  if (installed) return;
  installed = true;

  const origError = console.error.bind(console);
  console.error = (...args: unknown[]) => {
    post("error", args);
    origError(...args);
  };

  const origWarn = console.warn.bind(console);
  console.warn = (...args: unknown[]) => {
    post("warn", args);
    origWarn(...args);
  };

  window.addEventListener("error", (e) => {
    post("window.error", [e.message, `${e.filename}:${e.lineno}:${e.colno}`, e.error]);
  });

  window.addEventListener("unhandledrejection", (e) => {
    post("unhandledrejection", [e.reason]);
  });

  post("info", ["error relay installed"]);
}
