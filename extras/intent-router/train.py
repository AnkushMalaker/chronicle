"""Train the home-vs-chat intent router (Model2Vec potion-32M + logistic regression).

Run (standalone, no backend deps needed):
    uv run --with model2vec --with scikit-learn --with numpy \
        python3 plugins/homeassistant/intent_router/train.py

Outputs (saved next to this file):
    router_clf.joblib   - sklearn LogisticRegression head + label list + metadata
The Model2Vec encoder itself is loaded by name at runtime (cached by HF).
"""

import json
import time
from collections import Counter
from pathlib import Path

import numpy as np
from dataset import AMBIGUOUS_WATCH, REAL_PHRASES, build_dataset
from model2vec import StaticModel
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import StratifiedKFold, cross_val_predict

HERE = Path(__file__).parent
MODEL_NAME = "minishlab/potion-base-32M"
CLF_PATH = HERE / "router_clf.joblib"


def main():
    print(f"Loading encoder {MODEL_NAME} ...")
    t = time.time()
    enc = StaticModel.from_pretrained(MODEL_NAME)
    print(f"  loaded in {time.time()-t:.2f}s, dim={enc.dim}")

    texts, labels = build_dataset()
    print(f"Dataset: {len(texts)} examples  {Counter(labels)}")

    print("Encoding training set ...")
    X = np.asarray(enc.encode(texts))
    classes = sorted(set(labels))  # ['home', 'other']
    y = np.array([classes.index(l) for l in labels])
    home_idx = classes.index("home")

    # ---- cross-validated accuracy (honest, no leakage) ----
    print("\n=== 5-fold cross-validation ===")
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    base = LogisticRegression(max_iter=2000, C=4.0, class_weight="balanced")
    y_pred = cross_val_predict(base, X, y, cv=skf)
    print(classification_report(y, y_pred, target_names=classes, digits=3))
    print(f"CV accuracy: {accuracy_score(y, y_pred):.3f}")

    # ---- fit final model on everything ----
    clf = LogisticRegression(max_iter=2000, C=4.0, class_weight="balanced")
    clf.fit(X, y)

    # ---- evaluate on the user's REAL held-out phrases ----
    print("\n=== Held-out REAL phrases (the goal-line test set) ===")
    rp_texts = [t for t, _ in REAL_PHRASES]
    rp_true = [lab for _, lab in REAL_PHRASES]
    rp_X = np.asarray(enc.encode(rp_texts))
    rp_proba = clf.predict_proba(rp_X)[:, home_idx]
    correct = 0
    for txt, true, p in zip(rp_texts, rp_true, rp_proba):
        pred = "home" if p >= 0.5 else "other"
        ok = "OK " if pred == true else "XX "
        correct += pred == true
        print(f"  {ok} P(home)={p:0.2f}  pred={pred:5s} true={true:5s}  {txt}")
    print(
        f"REAL-phrase accuracy: {correct}/{len(rp_texts)} = {correct/len(rp_texts):.2f}"
    )

    # ---- observe ambiguous phrasings (not scored) ----
    print("\n=== Ambiguous watch (observe only, not scored) ===")
    amb_X = np.asarray(enc.encode(AMBIGUOUS_WATCH))
    amb_p = clf.predict_proba(amb_X)[:, home_idx]
    for txt, p in zip(AMBIGUOUS_WATCH, amb_p):
        print(f"  P(home)={p:0.2f}  {'home' if p>=0.5 else 'other'}  {txt}")

    # ---- latency benchmark (encode + classify, warm) ----
    print("\n=== Latency benchmark (CPU, warm) ===")
    sample = ["make it more soothing for my eyes"]
    for _ in range(50):
        clf.predict_proba(np.asarray(enc.encode(sample)))
    N = 2000
    t = time.time()
    for _ in range(N):
        clf.predict_proba(np.asarray(enc.encode(sample)))
    dt = (time.time() - t) / N * 1000
    print(f"  end-to-end (encode+logreg): {dt:.3f} ms/call over {N} calls")

    # ---- save ----
    import joblib

    joblib.dump(
        {
            "clf": clf,
            "classes": classes,
            "home_index": home_idx,
            "model_name": MODEL_NAME,
            "trained_examples": len(texts),
        },
        CLF_PATH,
    )
    print(f"\nSaved classifier -> {CLF_PATH}")
    # also drop a small metrics file for STATUS.md
    (HERE / "train_metrics.json").write_text(
        json.dumps(
            {
                "cv_accuracy": float(accuracy_score(y, y_pred)),
                "real_phrase_accuracy": correct / len(rp_texts),
                "latency_ms": dt,
                "n_examples": len(texts),
                "class_counts": dict(Counter(labels)),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
