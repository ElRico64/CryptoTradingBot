"""A small logistic-regression model trained on the bot's own trade journal.

Every analysed signal is stored with its features; every closed trade labels its signal with
the realised outcome. After enough trades, this model learns which feature combinations
actually paid in *your* market regime and blends its probability into the rule-based score.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from .features import MODEL_FEATURES


def model_inputs(features: dict[str, float]) -> list[float]:
    f = dict(features)
    f["log_liquidity"] = math.log10(max(f.get("liquidity_usd", 0.0), 1.0))
    f["log_mcap"] = math.log10(max(f.get("mcap_usd", 0.0), 1.0))
    f["log_age"] = math.log10(max(f.get("age_min", 0.0), 1.0))
    # Clip heavy-tailed percentages so one 5000% candle can't dominate the fit.
    for k in ("pc_m5", "pc_h1"):
        f[k] = max(-100.0, min(500.0, f.get(k, 0.0)))
    return [float(f.get(name, 0.0)) for name in MODEL_FEATURES]


class LogisticModel:
    def __init__(self, weights: np.ndarray, bias: float, mean: np.ndarray, std: np.ndarray, n_samples: int):
        self.weights = weights
        self.bias = bias
        self.mean = mean
        self.std = std
        self.n_samples = n_samples

    @classmethod
    def fit(cls, X: np.ndarray, y: np.ndarray, l2: float = 0.5, lr: float = 0.1, epochs: int = 3000) -> "LogisticModel":
        mean = X.mean(axis=0)
        std = X.std(axis=0) + 1e-9
        Z = (X - mean) / std
        w = np.zeros(Z.shape[1])
        b = 0.0
        n = len(y)
        for _ in range(epochs):
            p = 1 / (1 + np.exp(-(Z @ w + b)))
            grad_w = Z.T @ (p - y) / n + l2 * w / n
            grad_b = float((p - y).mean())
            w -= lr * grad_w
            b -= lr * grad_b
        return cls(w, b, mean, std, n)

    def predict_proba(self, features: dict[str, float]) -> float:
        x = (np.array(model_inputs(features)) - self.mean) / self.std
        return float(1 / (1 + math.exp(-(x @ self.weights + self.bias))))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                {
                    "features": MODEL_FEATURES,
                    "weights": self.weights.tolist(),
                    "bias": self.bias,
                    "mean": self.mean.tolist(),
                    "std": self.std.tolist(),
                    "n_samples": self.n_samples,
                },
                indent=2,
            )
        )

    @classmethod
    def load(cls, path: str | Path) -> "LogisticModel | None":
        p = Path(path)
        if not p.exists():
            return None
        d = json.loads(p.read_text())
        if d.get("features") != MODEL_FEATURES:
            return None  # feature set changed; retrain
        return cls(np.array(d["weights"]), float(d["bias"]), np.array(d["mean"]), np.array(d["std"]), int(d["n_samples"]))

    def explain(self) -> list[tuple[str, float]]:
        return sorted(zip(MODEL_FEATURES, self.weights.tolist()), key=lambda t: -abs(t[1]))
