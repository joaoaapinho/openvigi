"""Confidence intervals and the Jev price used by both evaluators."""
import math

PRICE_PER_M = 0.042  # $ per million Jev input tokens


def wilson(k, n, z=1.96):
    """95% confidence interval for a rate of k out of n (works near 0% and 100%, unlike +-1.96*SE)."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def poisson_upper(k, conf=0.95):
    """Upper bound on the expected number of events when k were seen. k=0 gives 3.0 (rule of three)."""
    def cdf(lam):
        return sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k + 1))

    lo, hi = 0.0, 10.0 + 5 * k
    for _ in range(60):  # bisection
        mid = (lo + hi) / 2
        if cdf(mid) > 1 - conf:
            lo = mid
        else:
            hi = mid
    return hi
