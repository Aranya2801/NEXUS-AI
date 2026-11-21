"""Small, dependency-free statistics used for A/B analysis and quality monitoring. Plain normal-approximation tests: valid for reasonably large samples and
independent observations; callers enforce minimum sample sizes and say so when data is insufficient."""
from __future__ import annotations

import math
import statistics


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float]:
    """Pooled two-sided z-test. Returns (z, p). p=1 when undefined (empty arm or no variance)."""
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0
    pool = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pool * (1 - pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (k1 / n1 - k2 / n2) / se
    return z, math.erfc(abs(z) / math.sqrt(2))


def welch_z(a: list[float], b: list[float]) -> tuple[float, float]:
    """Difference of means (a - b), normal approximation to Welch's t (fine for n >= ~30 per arm). Returns (z, p)."""
    if len(a) < 2 or len(b) < 2:
        return 0.0, 1.0
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    if se == 0:         # both arms constant: any difference in means is exact, not noise
        return (0.0, 1.0) if statistics.fmean(a) == statistics.fmean(b) else (math.copysign(math.inf, statistics.fmean(a) - statistics.fmean(b)), 0.0)
    z = (statistics.fmean(a) - statistics.fmean(b)) / se
    return z, math.erfc(abs(z) / math.sqrt(2))
