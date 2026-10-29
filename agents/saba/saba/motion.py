"""Smooth, reproducible pointer trajectories for authorized UI demos.

Coordinates are in pixels; time is seconds from movement start.
No browser automation or anti-detection behavior is included.
"""
from dataclasses import dataclass
import math
import random


@dataclass(frozen=True)
class Point:
    t: float
    x: float
    y: float


def cubic_bezier(p0, p1, p2, p3, t):
    """Evaluate a cubic Bézier curve at t in [0, 1]."""
    if not 0 <= t <= 1:
        raise ValueError("t must be between 0 and 1")
    u = 1 - t
    return (
        u**3 * p0[0] + 3*u*u*t*p1[0] + 3*u*t*t*p2[0] + t**3*p3[0],
        u**3 * p0[1] + 3*u*u*t*p1[1] + 3*u*t*t*p2[1] + t**3*p3[1],
    )


def _ease(t):
    """Quintic smootherstep, zero velocity and acceleration at endpoints."""
    return t*t*t*(t*(t*6 - 15) + 10)


def generate_path(start, end, *, duration=0.65, fps=60, curvature=0.18, seed=None):
    """Generate a smooth Bézier trajectory with reproducible variation.

    The same inputs and seed yield the same path. Endpoint coordinates
    are exact. Use duration/fps to tune screen-recording playback.
    """
    if duration <= 0 or not math.isfinite(duration):
        raise ValueError("duration must be finite and positive")
    if not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
        raise ValueError("fps must be finite and positive")
    if not math.isfinite(curvature) or not 0 <= curvature <= 1:
        raise ValueError("curvature must be between 0 and 1")
    if len(start) != 2 or len(end) != 2:
        raise ValueError("start and end must be coordinate pairs")
    if not all(math.isfinite(v) for v in (*start, *end)):
        raise ValueError("coordinates must be finite")

    rng = random.Random(seed)
    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = math.hypot(dx, dy)
    if distance == 0:
        return [Point(0.0, float(start[0]), float(start[1]))]
    nx, ny = -dy / distance, dx / distance
    bend = distance * curvature * rng.uniform(-1, 1)
    p1 = (start[0] + dx * 0.32 + nx*bend,
          start[1] + dy * 0.32 + ny*bend)
    p2 = (start[0] + dx * 0.70 + nx*bend*0.65,
          start[1] + dy * 0.70 + ny*bend*0.65)
    samples = max(2, math.ceil(duration * fps) + 1)
    result = []
    for i in range(samples):
        fraction = i / (samples - 1)
        x, y = cubic_bezier(start, p1, p2, end, _ease(fraction))
        result.append(Point(duration*fraction, x, y))
    result[0] = Point(0.0, float(start[0]), float(start[1]))
    result[-1] = Point(duration, float(end[0]), float(end[1]))
    return result
