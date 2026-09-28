"""Resolve canonical DATA roots for the ANALYTICS forecast engine.

The ANALYTICS repository is a sibling of DATA. Runtime data reads must use
this resolver instead of a repository-local research tree.
"""
from dataclasses import dataclass
from pathlib import Path
import sys


CANONICAL_DATA_PATHS = {
    "raw": Path("canonical/research/raw"),
    "derived": Path("canonical/research/derived"),
    "trackers": Path("canonical/research/trackers"),
    "federal": Path("canonical/research/federal"),
    "geo": Path("geo"),
    "states": Path("canonical/research/states"),
}


class DataRootResolutionError(RuntimeError):
    """Raised when the required sibling DATA repository is incomplete."""


@dataclass(frozen=True)
class CanonicalDataRoots:
    """Validated locations of the canonical DATA domains."""

    repository_root: Path
    data_root: Path
    raw: Path
    derived: Path
    trackers: Path
    federal: Path
    geo: Path
    states: Path

    def path(self, domain):
        """Return a named canonical data domain."""
        try:
            return getattr(self, domain)
        except AttributeError as error:
            raise KeyError("Unknown canonical data domain: {}".format(domain)) from error


def resolve_repository_root(anchor=None):
    """Find the V3 monorepo root from an engine file, independent of cwd.

    V3 change (P1.6/P1.7): delegates to the monorepo-wide v3_paths resolver
    instead of counting __file__ parents against the historical layout.
    """
    anchor_path = Path(anchor or __file__).resolve()
    try:
        sys.path.insert(0, str(anchor_path.parents[1]))
        import v3_paths

        return v3_paths.project_root(anchor_path)
    finally:
        sys.path.pop(0)


def resolve_data_roots(repository_root=None, data_root=None):
    """Return validated canonical DATA locations for a V3 monorepo checkout.

    V3 change (P1.6/P1.7): data lives inside the monorepo at <root>/data —
    the V2 sibling ``1_DATA`` layout is gone. ``data_root`` override is still
    honoured (tests). DATA remains deliberately required: no compatibility
    fallback to a repository-local research tree is permitted.
    """
    repository = Path(repository_root).resolve() if repository_root else resolve_repository_root()
    if data_root:
        data = Path(data_root).resolve()
    else:
        try:
            sys.path.insert(0, str(repository))
            import v3_paths

            data = v3_paths.data_root(repository)
        finally:
            sys.path.pop(0)

    if not data.is_dir():
        raise DataRootResolutionError("Canonical DATA root is missing: {}".format(data))

    resolved = {}
    for domain, relative_path in CANONICAL_DATA_PATHS.items():
        mapped_path = data / relative_path
        if not mapped_path.is_dir():
            raise DataRootResolutionError(
                "Canonical DATA mapping '{}' is missing: {}".format(domain, mapped_path)
            )
        resolved[domain] = mapped_path

    return CanonicalDataRoots(
        repository_root=repository,
        data_root=data,
        **resolved
    )
