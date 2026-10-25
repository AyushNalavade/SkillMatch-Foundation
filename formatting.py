"""Small display helpers for numeric values stored as JSON numbers."""

import math
from typing import Any


def format_decimal(value: Any) -> str:
    """Display numbers to two decimals without trailing zeros or binary noise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    number = float(value)
    if not math.isfinite(number):
        return str(value)
    rounded = round(number, 2)
    if rounded == 0 and number != 0:
        return f"{number:.12g}"
    return f"{rounded:.2f}".rstrip("0").rstrip(".")
