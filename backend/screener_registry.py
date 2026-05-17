from __future__ import annotations

"""Discover and validate screener modules.

The registry is what turns Python files in `screeners/` into dropdown options
in Streamlit. It also protects the rest of the app by checking every screener
uses the same small contract before the user can run it.
"""

import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from types import ModuleType
from typing import Callable

import pandas as pd


# Every screener must include these metadata fields in its SCREENER dict. They
# tell the UI what to show and tell the backend which universe/lookback to use.
REQUIRED_METADATA_KEYS = ("key", "name", "description", "universe", "timeframe", "lookback_days")


class ScreenerRegistryError(ValueError):
    """Raised when a screener module does not follow the required contract."""

    pass


@dataclass(frozen=True)
class ScreenerDefinition:
    """Validated screener metadata plus the callable run function.

    `build_chart` is optional: a screener can omit it (older or experimental
    screeners). When None, the Streamlit UI hides the per-stock chart section.
    """

    key: str
    name: str
    description: str
    universe: str
    timeframe: str
    lookback_days: int
    default_params: dict
    module_name: str
    run: Callable
    build_chart: Callable | None = None


def validate_screener_module(module: ModuleType) -> ScreenerDefinition:
    """Check one Python module and convert it into a ScreenerDefinition."""
    metadata = getattr(module, "SCREENER", None)
    if not isinstance(metadata, dict):
        raise ScreenerRegistryError(f"{module.__name__} must define SCREENER as a dict")

    # Fail fast on missing metadata. This gives a future screener author a clear
    # error message instead of a mysterious Streamlit dropdown failure.
    missing = [key for key in REQUIRED_METADATA_KEYS if key not in metadata]
    if missing:
        raise ScreenerRegistryError(f"{module.__name__} SCREENER missing: {', '.join(missing)}")

    run_func = getattr(module, "run", None)
    if not callable(run_func):
        raise ScreenerRegistryError(f"{module.__name__} must define callable run(...)")

    signature = inspect.signature(run_func)
    expected = ["universe_df", "data_loader", "params"]
    # The parameter names are part of the teaching/documentation value here:
    # new screeners can copy an existing screener's run(...) signature exactly.
    if list(signature.parameters)[:3] != expected:
        raise ScreenerRegistryError(
            f"{module.__name__}.run must accept (universe_df, data_loader, params)"
        )

    return_type = signature.return_annotation
    # We allow no return annotation for convenience, but if one is present it
    # should advertise that the screener returns a pandas DataFrame.
    if return_type not in (inspect.Signature.empty, pd.DataFrame, "pd.DataFrame"):
        raise ScreenerRegistryError(f"{module.__name__}.run should return a pandas DataFrame")

    # `build_chart` is purely optional. We do not validate its signature so
    # screener authors are free to keep it minimal or accept extra kwargs.
    build_chart_func = getattr(module, "build_chart", None)
    if build_chart_func is not None and not callable(build_chart_func):
        raise ScreenerRegistryError(f"{module.__name__}.build_chart must be callable if defined")

    return ScreenerDefinition(
        key=str(metadata["key"]),
        name=str(metadata["name"]),
        description=str(metadata["description"]),
        universe=str(metadata["universe"]),
        timeframe=str(metadata["timeframe"]),
        lookback_days=int(metadata["lookback_days"]),
        default_params=dict(metadata.get("default_params", {})),
        module_name=module.__name__,
        run=run_func,
        build_chart=build_chart_func,
    )


def discover_screeners(package_name: str = "screeners") -> dict[str, ScreenerDefinition]:
    """Import every public module in the screeners package and validate it.

    Beginner note:
    `importlib.import_module` is the programmatic equivalent of typing
    `import screeners.my_screener`. We use it (with a fixed package name, never
    user input) so adding a new file under `screeners/` automatically shows up
    in the UI without editing this registry or `app.py`.
    """
    package = importlib.import_module(package_name)
    screeners: dict[str, ScreenerDefinition] = {}

    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.name.startswith("_"):
            # Leading underscore means "private/helper module" by Python
            # convention, so it should not appear in the dropdown.
            continue
        module = importlib.import_module(f"{package_name}.{module_info.name}")
        definition = validate_screener_module(module)
        if definition.key in screeners:
            # Duplicate keys would make result filenames and lookup behavior
            # ambiguous (which screener owns `bb_reversal_results.csv`?), so we
            # refuse to load both rather than guess.
            raise ScreenerRegistryError(f"Duplicate screener key: {definition.key}")
        screeners[definition.key] = definition

    # Sorting by display name gives a stable, friendly dropdown order.
    return dict(sorted(screeners.items(), key=lambda item: item[1].name.lower()))
