"""Single source of truth for the release version (SemVer). `infrastructure/scripts/release.py bump X.Y.Z` updates this and every
place that must agree with it; `release.py check` (run in CI) fails if they drift."""
__version__ = "0.1.0"
