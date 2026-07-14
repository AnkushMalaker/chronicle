FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the static embedding model into the image so the service does
# NOT depend on Hugging Face being reachable at runtime (offline-safe; avoids
# the hf_hub transient-DNS crash-loop seen elsewhere in the project).
ENV HF_HOME=/opt/hf
RUN python -c "from model2vec import StaticModel; StaticModel.from_pretrained('minishlab/potion-base-32M')"

COPY router.py router_clf.joblib app.py ./
# Training assets (optional at runtime, handy for retraining inside the image)
COPY dataset.py train.py ./

ENV HF_HUB_OFFLINE=1
ENV INTENT_ROUTER_PORT=8791
EXPOSE 8791

CMD ["python", "app.py"]
