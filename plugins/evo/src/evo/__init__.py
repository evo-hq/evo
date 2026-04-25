"""evo package."""

__all__ = ["__version__", "DISTRIBUTION_NAME"]

# PyPI distribution name. Stable string used by skill checks to distinguish
# our binary from unrelated `evo` packages on PATH (e.g. the SLAM tool).
DISTRIBUTION_NAME = "evo-hq-cli"
__version__ = "0.3.0"
