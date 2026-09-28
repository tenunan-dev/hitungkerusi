"""Repository-independent locations for generated ANALYTICS working artifacts."""

from pathlib import Path


def resolve_repository_root(anchor=None):
    """Return the ANALYTICS root without relying on its historical basename."""
    return Path(anchor or __file__).resolve().parents[1]


def work_root(repository_root=None):
    """Return the generated-artifact root, optionally for an explicit repository."""
    root = Path(repository_root) if repository_root is not None else resolve_repository_root()
    return root / "work"


def forecast_latest(repository_root=None):
    return work_root(repository_root) / "forecast" / "latest"


def reports_latest(repository_root=None):
    return work_root(repository_root) / "reports" / "latest"


def social_current(repository_root=None):
    return work_root(repository_root) / "social" / "current"


def tracking_root(repository_root=None):
    return work_root(repository_root) / "tracking"
