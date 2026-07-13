"""Runtime home-vs-chat intent router.

Sub-millisecond local classifier (Model2Vec potion-32M static embedding +
logistic-regression head). Decides whether a transcribed voice command is a
home-automation request ("home") or a general agent/chat query ("other").

Usage:
    from intent_router.router import get_router
    router = get_router()
    result = router.classify("make it more soothing for my eyes")
    # result -> RouteResult(route='home', p_home=0.93, label='home')

Fail-open policy: if the model/artifacts can't load, we default to route='home'
so the request still enters the HA cascade, which itself falls back to Hermes.
A hard failure here therefore degrades latency, never correctness.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
CLF_PATH = HERE / "router_clf.joblib"
DEFAULT_MODEL = "minishlab/potion-base-32M"

# Below this P(home) we route to the agent ("other"). Tunable via env.
# Bias note: a false 'home' is cheap (the HA cascade self-corrects to Hermes),
# a false 'other' wastes the slow agent path - so leaning the threshold down
# slightly is reasonable. Default 0.5.
HOME_THRESHOLD = float(os.getenv("ROUTER_HOME_THRESHOLD", "0.5"))


@dataclass
class RouteResult:
    route: str  # "home" | "other"
    p_home: float
    label: str  # raw argmax label
    latency_ms: float
    ok: bool = True  # False if we fell back to default due to load failure


class IntentRouter:
    def __init__(self, threshold: float = HOME_THRESHOLD):
        self.threshold = threshold
        self._enc = None
        self._clf = None
        self._classes = None
        self._home_index = 0
        self._loaded = False
        self._load_error: Optional[str] = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> bool:
        if self._loaded:
            return self._clf is not None
        with self._lock:
            if self._loaded:
                return self._clf is not None
            try:
                # Lazy import: deliberate — fail-open policy (see module docstring).
                # Model/artifact deps only need to load on first real classify() call.
                import joblib
                import numpy as np  # noqa: F401  (imported for side-effect/availability)
                from model2vec import StaticModel

                bundle = joblib.load(CLF_PATH)
                model_name = bundle.get("model_name", DEFAULT_MODEL)
                self._enc = StaticModel.from_pretrained(model_name)
                self._clf = bundle["clf"]
                self._classes = bundle["classes"]
                self._home_index = bundle["home_index"]
                logger.info(
                    "IntentRouter loaded (%s, %d examples, threshold=%.2f)",
                    model_name,
                    bundle.get("trained_examples", -1),
                    self.threshold,
                )
            except Exception as e:  # pragma: no cover - environment dependent
                self._load_error = str(e)
                logger.error(
                    "IntentRouter failed to load (%s); failing open to 'home'", e
                )
            finally:
                self._loaded = True
        return self._clf is not None

    def warm(self) -> None:
        """Load + run one inference so the first real call is fast."""
        if self._ensure_loaded():
            self.classify("turn off the lights")

    def classify(self, text: str) -> RouteResult:
        t0 = time.time()
        if not text or not text.strip():
            return RouteResult("other", 0.0, "other", 0.0, ok=True)

        if not self._ensure_loaded():
            # fail open -> home cascade (which self-corrects to Hermes)
            return RouteResult("home", 1.0, "home", (time.time() - t0) * 1000, ok=False)

        # Lazy import: deliberate — same fail-open policy as _ensure_loaded above.
        import numpy as np

        vec = np.asarray(self._enc.encode([text]))
        proba = self._clf.predict_proba(vec)[0]
        p_home = float(proba[self._home_index])
        label = self._classes[int(np.argmax(proba))]
        route = "home" if p_home >= self.threshold else "other"
        return RouteResult(route, p_home, label, (time.time() - t0) * 1000, ok=True)


_router: Optional[IntentRouter] = None
_router_lock = threading.Lock()


def get_router() -> IntentRouter:
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                _router = IntentRouter()
    return _router


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = get_router()
    r.warm()
    for t in [
        "make it more soothing for my eyes",
        "turn off the hall lights",
        "what's the meaning of life",
        "remind me to buy milk",
        "set evening mode",
    ]:
        res = r.classify(t)
        print(f"{res.route:5s}  P(home)={res.p_home:0.2f}  {res.latency_ms:.2f}ms  {t}")
