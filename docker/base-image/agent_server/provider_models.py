"""Qualified model ids used for per-task provider routing (PULSIT-MODELS-001).

Stdlib-only. Vendored byte-identically into agent_server/provider_models.py.
"""
import re

_QUALIFIED = re.compile(
    r"[a-z][a-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,191}"
)


def is_provider_task_model(model: str | None) -> bool:
    """A provider/model is one argv value, never a URL, option or path."""
    return isinstance(model, str) and bool(_QUALIFIED.fullmatch(model)) and ".." not in model
