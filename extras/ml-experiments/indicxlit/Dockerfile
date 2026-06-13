FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends g++ && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "torch<2.6" \
    ai4bharat-transliteration \
    flask \
    gunicorn

COPY server.py /app/server.py
WORKDIR /app

# Pre-download model on build
RUN python -c "from ai4bharat.transliteration import XlitEngine; XlitEngine(src_script_type='indic', beam_width=4)" || true

EXPOSE 5000
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--timeout", "300", "server:app"]
