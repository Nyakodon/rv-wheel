"""Pure maths: raw wheel/pedal values -> normalised controls."""
from __future__ import annotations


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def steer_norm(raw: int, s: dict) -> float:
    """Raw wheel axis value -> steering in [-1, 1] (negative = left), shaped by deadband/expo/gain."""
    center, left, right = s["center"], s["left"], s["right"]
    d = raw - center
    if d == 0:
        x = 0.0
    elif (d > 0) == (right > center):
        span = abs(right - center)
        x = abs(d) / span if span else 0.0
    else:
        span = abs(left - center)
        x = -abs(d) / span if span else 0.0
    x = clamp(x, -1.0, 1.0)

    if abs(x) < s["deadband"]:
        return 0.0
    mag = clamp(abs(x) ** s["expo"] * s["gain"], 0.0, 1.0)
    return mag if x > 0 else -mag


def apply_game_deadzone(x: float, deadzone: float) -> float:
    """Land just outside a downstream deadzone (virtual-pad mode)."""
    if deadzone <= 0.0 or x == 0.0:
        return x
    mag = deadzone + (1.0 - deadzone) * abs(x)
    return mag if x > 0 else -mag


def pedal_norm(raw: int, p: dict) -> float:
    """Raw pedal axis value -> travel in [0, 1], regardless of axis polarity."""
    rest, full = p["rest"], p["full"]
    if rest == full:
        return 0.0
    return clamp((raw - rest) / (full - rest), 0.0, 1.0)
