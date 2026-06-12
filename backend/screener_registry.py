"""Discover and validate screener modules.

The registry is what turns Python files in `screeners/` into dropdown options
in Streamlit. It also protects the rest of the app by checking every screener
uses the same small contract before the user can run it.

Two patterns are accepted:

1. **Class-based** (preferred). The module defines a `BaseScanner` subclass.
   The registry finds the class, instantiates it, and pulls metadata from
   the instance's `SCREENER` dict. New screeners should use this pattern.

2. **Module-based** (legacy). The module exposes `SCREENER`, `run`, and an
   optional `build_chart` at the top level. The registry still accepts this
   shape, both for backwards compatibility with older tests and so anyone
   following the existing pattern is not forced to refactor at once.

Both paths produce the same `ScreenerDefinition` dataclass, so the rest of
the app (Streamlit UI, tests) does not need to care which style a screener
used.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType

import pandas as pd

from backend.scanner_base import BaseScanner

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


def _find_scanner_class(module: ModuleType) -> type[BaseScanner] | None:
    """Return the first `BaseScanner` subclass defined inside `module`, or None.

    Why "defined inside": importing `BaseScanner` into the module's namespace
    should not make it a screener. We only count classes whose `__module__`
    matches the file we are inspecting.
    """
    for attribute in vars(module).values():
        if (
            inspect.isclass(attribute)
            and issubclass(attribute, BaseScanner)
            and attribute is not BaseScanner
            and attribute.__module__ == module.__name__
        ):
            return attribute
    return None


def _validate_metadata(metadata: object, module_name: str) -> dict:
    """Confirm a SCREENER dict has every required key. Raise otherwise."""
    if not isinstance(metadata, dict):
        raise ScreenerRegistryError(f"{module_name} must define SCREENER as a dict")
    # Fail fast on missing metadata. This gives a future screener author a clear
    # error message instead of a mysterious Streamlit dropdown failure.
    missing = [key for key in REQUIRED_METADATA_KEYS if key not in metadata]
    if missing:
        raise ScreenerRegistryError(f"{module_name} SCREENER missing: {', '.join(missing)}")
    return metadata


def _validate_run_signature(run_func: object, module_name: str) -> None:
    """Confirm `run` is callable and takes (universe_df, data_loader, params)."""
    if not callable(run_func):
        raise ScreenerRegistryError(f"{module_name} must define callable run(...)")

    signature = inspect.signature(run_func)
    # The parameter names are part of the teaching/documentation value here:
    # new screeners can copy an existing screener's run(...) signature exactly.
    # For bound methods, `self` is already excluded from `signature.parameters`.
    expected = ["universe_df", "data_loader", "params"]
    if list(signature.parameters)[:3] != expected:
        raise ScreenerRegistryError(
            f"{module_name}.run must accept (universe_df, data_loader, params)"
        )

    return_type = signature.return_annotation
    # We allow no return annotation for convenience, but if one is present it
    # should advertise that the screener returns a pandas DataFrame.
    if return_type not in (inspect.Signature.empty, pd.DataFrame, "pd.DataFrame"):
        raise ScreenerRegistryError(f"{module_name}.run should return a pandas DataFrame")


def validate_screener_module(module: ModuleType) -> ScreenerDefinition:
    """Check one Python module and convert it into a ScreenerDefinition.

    Beginner note:
    Both class-based and module-based screeners pass through this single
    function. The branch below figures out which style is in use so the rest
    of the app sees a uniform `ScreenerDefinition` either way.
    """
    scanner_class = _find_scanner_class(module)
    if scanner_class is not None:
        # ---- Class-based screener (preferred) ----
        try:
            instance = scanner_class()
        except TypeError as exc:
            # Most likely cause: a subclass forgot to implement compute_signal,
            # so Python refuses to instantiate the abstract class.
            raise ScreenerRegistryError(
                f"{module.__name__}: cannot instantiate {scanner_class.__name__}: {exc}"
            ) from exc

        metadata = _validate_metadata(getattr(instance, "SCREENER", None), module.__name__)
        # `run` is a bound method on the instance. Inspect treats it like a
        # regular callable that drops `self`, so signature validation works.
        run_func: Callable = instance.run
        _validate_run_signature(run_func, module.__name__)

        # `build_chart` on `BaseScanner` is always defined (default returns
        # None). Only register it on the definition if a subclass actually
        # overrode it, so the UI can keep hiding the chart pane when not needed.
        if type(instance).build_chart is BaseScanner.build_chart:
            build_chart_func: Callable | None = None
        else:
            build_chart_func = instance.build_chart
    else:
        # ---- Legacy module-based screener ----
        metadata = _validate_metadata(getattr(module, "SCREENER", None), module.__name__)
        run_func = getattr(module, "run", None)
        _validate_run_signature(run_func, module.__name__)

        # `build_chart` is purely optional. We do not validate its signature so
        # screener authors are free to keep it minimal or accept extra kwargs.
        build_chart_func = getattr(module, "build_chart", None)
        if build_chart_func is not None and not callable(build_chart_func):
            raise ScreenerRegistryError(
                f"{module.__name__}.build_chart must be callable if defined"
            )

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
