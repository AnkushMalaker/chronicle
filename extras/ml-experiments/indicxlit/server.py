"""Lightweight IndicXlit transliteration service.

Endpoints:
    POST /romanize  {"text": "नमस्ते दुनिया", "topk": 1}
        → {"result": "namaste duniya", "words": [{"src": "नमस्ते", "dst": ["namaste"]}, ...]}

    GET /health → {"status": "ok"}
"""

import re

from flask import Flask, jsonify, request

app = Flask(__name__)

# Lazy init
_engine = None
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")


def get_engine():
    global _engine
    if _engine is None:
        # Imported on first use: loading the transliteration engine is expensive.
        from ai4bharat.transliteration import XlitEngine

        _engine = XlitEngine(src_script_type="indic", beam_width=4, rescore=True)
    return _engine


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/romanize", methods=["POST"])
def romanize():
    data = request.json or {}
    text = data.get("text", "")
    topk = data.get("topk", 1)
    lang = data.get("lang", "hi")

    if not text.strip():
        return jsonify({"result": "", "words": []})

    engine = get_engine()
    words = text.split()
    result_words = []
    out_parts = []

    for w in words:
        if _DEVANAGARI_RE.search(w):
            candidates = engine.translit_word(w, lang_code=lang, topk=topk)
            if isinstance(candidates, list):
                dst = candidates
            elif isinstance(candidates, dict):
                dst = candidates.get(lang, [w])
            else:
                dst = [str(candidates)]
            out_parts.append(dst[0] if dst else w)
            result_words.append({"src": w, "dst": dst})
        else:
            # Already Roman — pass through
            out_parts.append(w)
            result_words.append({"src": w, "dst": [w]})

    return jsonify(
        {
            "result": " ".join(out_parts),
            "words": result_words,
        }
    )


@app.route("/romanize_batch", methods=["POST"])
def romanize_batch():
    """Batch romanize multiple texts."""
    data = request.json or {}
    texts = data.get("texts", [])
    topk = data.get("topk", 1)
    lang = data.get("lang", "hi")

    engine = get_engine()
    results = []

    for text in texts:
        words = text.split()
        out_parts = []
        for w in words:
            if _DEVANAGARI_RE.search(w):
                candidates = engine.translit_word(w, lang_code=lang, topk=topk)
                if isinstance(candidates, list):
                    out_parts.append(candidates[0] if candidates else w)
                elif isinstance(candidates, dict):
                    vals = candidates.get(lang, [w])
                    out_parts.append(vals[0] if vals else w)
                else:
                    out_parts.append(w)
            else:
                out_parts.append(w)
        results.append(" ".join(out_parts))

    return jsonify({"results": results})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
