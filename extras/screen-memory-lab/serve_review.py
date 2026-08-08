"""Serve the review page and accept a real save, so answers land on disk.

A page opened over file:// cannot write anywhere, so the first version of the
review viewer could only offer a browser download -- which puts the file wherever
the browser decides. This serves the same directory over HTTP and accepts
``POST /save``, writing to ``out/review/answers.json`` where the analysis can read
it directly.

Bound to localhost only. It writes exactly one filename and ignores the client's
opinion about paths.

Run:
    uv run python serve_review.py
    # then open http://127.0.0.1:8823/
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "out" / "review"
ANSWERS = ROOT / "answers.json"
PORT = 8823
MAX_BODY = 8 * 1024 * 1024


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/") != "/save":
            self.send_error(404, "only /save accepts POST")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_error(400, "bad Content-Length")
            return
        if length <= 0 or length > MAX_BODY:
            self.send_error(413, "body missing or too large")
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.send_error(400, f"not JSON: {exc}")
            return

        # Keep a backup rather than overwriting blindly -- a review happens over
        # more than one sitting. But only keep one per hour: the first version
        # rotated on every POST and a UI bug that saved per keystroke left 130
        # files behind. Losing an earlier pass is the risk worth guarding; a
        # per-keystroke archive is not.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%HZ")
        backup = ROOT / f"answers-{stamp}.bak.json"
        if ANSWERS.exists() and not backup.exists():
            backup.write_bytes(ANSWERS.read_bytes())
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        ANSWERS.write_text(json.dumps(payload, indent=1))

        answers = payload.get("answers") or {}
        answered = sum(
            1
            for a in answers.values()
            if a.get("verdict")
            or a.get("comment")
            or any((a.get("fields") or {}).values())
        )
        print(f"saved {answered} answered items -> {ANSWERS}")

        body = json.dumps({"ok": True, "answered": answered, "path": str(ANSWERS)})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, fmt: str, *args) -> None:
        # Quieten the per-image GET flood but keep saves visible. args[0] is not
        # always a string -- send_response passes an HTTPStatus -- so coerce
        # before testing, or the handler raises inside its own logger.
        first = str(args[0]) if args else ""
        if "POST" in first:
            super().log_message(fmt, *args)


def main() -> None:
    if not (ROOT / "index.html").exists():
        raise SystemExit(f"no review page at {ROOT}; run make_review.py first")
    handler = partial(Handler, directory=str(ROOT))
    with ThreadingHTTPServer(("127.0.0.1", PORT), handler) as httpd:
        print(f"serving {ROOT}")
        print(f"open  http://127.0.0.1:{PORT}/")
        print(f"saves to {ANSWERS}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
