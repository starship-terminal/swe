"""SWE remote: watch and steer sessions from a phone (see docs/remote-control.md)."""

__all__ = ["cmd_remote"]


def __getattr__(name: str):
    # Imported on first use, not with the package: importing any
    # ``swe.remote.<module>`` runs this file, and ``commands`` pulls in
    # the whole hub. A session keeper (``swe.remote.keeper``) is one
    # process per live session and must not carry that: it cost each
    # keeper about 23 MB.
    if name == "cmd_remote":
        from swe.remote.commands import cmd_remote

        return cmd_remote
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
