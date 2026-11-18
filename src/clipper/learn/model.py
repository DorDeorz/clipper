"""L2-regularised logistic regression in numpy (ROADMAP 4.9 Kademe 2).

Newton / IRLS on standardised features. The prior mean of the weights can be set, so a
per-streamer model is shrunk toward the global one when it has little data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

Arr = NDArray[np.float64]


def sigmoid(z: Arr) -> Arr:
    out: Arr = 1.0 / (1.0 + np.exp(-np.clip(z, -35.0, 35.0)))
    return out


@dataclass
class LogReg:
    names: list[str]
    mean: Arr
    std: Arr
    w: Arr  # weights on standardised features
    b: float

    def decision(self, x: Arr) -> Arr:
        z: Arr = ((x - self.mean) / self.std) @ self.w + self.b
        return z

    def predict(self, x: Arr) -> Arr:
        return sigmoid(self.decision(x))

    def raw_weights(self) -> dict[str, float]:
        """Weights per unit of the original feature."""
        return {n: float(w / s) for n, w, s in zip(self.names, self.w, self.std, strict=True)}

    def to_json(self) -> dict[str, Any]:
        return {
            "names": self.names,
            "mean": self.mean.tolist(),
            "std": self.std.tolist(),
            "w": self.w.tolist(),
            "b": self.b,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> LogReg:
        return cls(
            list(d["names"]),
            np.asarray(d["mean"], dtype=float),
            np.asarray(d["std"], dtype=float),
            np.asarray(d["w"], dtype=float),
            float(d["b"]),
        )


def fit(
    x: Arr,
    y: Arr,
    names: list[str],
    *,
    l2: float = 1.0,
    sample_weight: Arr | None = None,
    prior: LogReg | None = None,
    prior_strength: float = 0.0,
    iters: int = 50,
) -> LogReg:
    """Minimise weighted log loss + l2/2·|w|² (+ prior_strength/2·|w − w_prior|²)."""
    n, d = x.shape
    if prior is not None:
        mean, std = prior.mean, prior.std
    else:
        mean = x.mean(axis=0)
        std = x.std(axis=0)
        std = np.where(std < 1e-9, 1.0, std)
    xs = (x - mean) / std
    a = np.hstack([xs, np.ones((n, 1))])
    sw = np.ones(n) if sample_weight is None else sample_weight.astype(float)
    # Penalty: base/2·|β|² + λ/2·|β − β_prior|²  ⇒  gradient reg·(β − target) with
    # reg = base + λ and target = λ·β_prior / reg.
    base = np.full(d + 1, l2)
    base[-1] = 1e-6  # (almost) no penalty on the intercept
    lam = prior_strength if prior is not None else 0.0
    reg = base + lam
    target = np.zeros(d + 1)
    if prior is not None and lam > 0:
        target = lam * np.append(prior.w, prior.b) / reg
    beta = np.zeros(d + 1)
    for _ in range(iters):
        p = sigmoid(a @ beta)
        grad = a.T @ (sw * (p - y)) + reg * (beta - target)
        hess = (a.T * (sw * p * (1 - p))) @ a + np.diag(reg)
        step = np.linalg.solve(hess, grad)
        beta -= step
        if float(np.max(np.abs(step))) < 1e-8:
            break
    return LogReg(names, mean, std, beta[:d].copy(), float(beta[-1]))


def roc_auc(y: Arr, score: Arr) -> float | None:
    """Rank-based AUC (ties averaged). None when only one class is present."""
    pos = y == 1
    n_pos, n_neg = int(pos.sum()), int((~pos).sum())
    if n_pos == 0 or n_neg == 0:
        return None
    order = np.argsort(score, kind="mergesort")
    ranks = np.empty(len(score))
    s = score[order]
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def precision_at_k(y: Arr, score: Arr, k: int) -> float | None:
    if len(y) == 0 or k <= 0:
        return None
    k = min(k, len(y))
    top = np.argsort(-score, kind="mergesort")[:k]
    return float(y[top].mean())


def log_loss(y: Arr, p: Arr) -> float:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
