"""swe — central manager for AI coding CLI tools.

``swe`` is both the command and the project (SWE Wraps Everything). The
values below are the single source of truth for the command name and the
on-disk data directory.
"""

__version__ = "1.2.3"

# User-facing CLI command name (the console entry point registered in
# pyproject.toml).
CLI_NAME = "swe"

# Name of the single root directory swe owns, at ~/.<DATA_DIR_NAME>. It
# holds the shared AGENTS.md and skills/ that harnesses symlink to, plus
# swe's own config and cache. Machines that already have a ~/.quiver get
# moved to ~/.swe on first run (see swe.paths._resolve_data_dir).
DATA_DIR_NAME = "swe"

# Deprecated alias. Was the directory name under ~/.config before 0.2.7.
CONFIG_DIR_NAME = DATA_DIR_NAME

__all__ = ["__version__", "CLI_NAME", "DATA_DIR_NAME", "CONFIG_DIR_NAME"]
