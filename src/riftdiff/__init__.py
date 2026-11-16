"""rift: local-first data diff, powered by DuckDB."""

__version__ = "0.1.1"

from .engine import DiffResult, RiftError, RowChange, diff  # noqa: E402

__all__ = ["diff", "DiffResult", "RowChange", "RiftError", "__version__"]
