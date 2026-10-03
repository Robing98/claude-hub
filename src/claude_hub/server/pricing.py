"""Turn token counts into an API-equivalent cost.

A subscription is a flat fee, so this is not what the work cost. It is what
the same tokens would cost on the API, which makes projects comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

# The order of the token counts everywhere: usage rows, parser output, costs.
FIELDS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
# Used when the file gives only the input price of a model.
MULTIPLIERS = {"cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1}


@dataclass
class Prices:
    models: dict[str, dict[str, float]] = field(default_factory=dict)
    source: str = ""
    read_on: str = ""
    plan_usd_per_month: float = 0.0
    error: str = ""

    def rates(self, model: str) -> dict[str, float] | None:
        """Find the prices of a model by the longest key that its ID starts with."""
        model = model.lower()
        best = max((key for key in self.models if model.startswith(key)), key=len, default=None)
        return self.models[best] if best else None

    def cost(self, model: str, counts: dict[str, int]) -> float | None:
        """Cost in USD, or None when the file has no price for the model."""
        rates = self.rates(model)
        if rates is None:
            return None
        return sum(counts.get(name, 0) * rates[name] for name in FIELDS) / 1_000_000


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def load_prices(path: Path | None) -> Prices:
    """Read the price file. It is read on every call, so an edit is live at once."""
    if path is None or not path.is_file():
        return Prices(error=f"No price file at {path}.")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Prices(error=f"{path}: {exc}")
    prices = Prices(source=str(data.get("source", "")), read_on=str(data.get("read_on", "")),
                    plan_usd_per_month=_number(data.get("plan_usd_per_month")) or 0.0)
    models = data.get("models")
    for key, entry in (models.items() if isinstance(models, dict) else ()):
        base = _number(entry.get("input")) if isinstance(entry, dict) else None
        output = _number(entry.get("output")) if isinstance(entry, dict) else None
        if base is None or output is None:
            continue
        rates = {"input": base, "output": output}
        for name, factor in MULTIPLIERS.items():
            given = _number(entry.get(name))
            rates[name] = given if given is not None else base * factor
        prices.models[str(key).lower()] = rates
    return prices
