"""The session keeper: one process per live SWE-started session.

- :mod:`.log`: the durable event log (``docs/specs/G4-keeper-event-log.md``)
- :mod:`.process`: the keeper itself, ``python -m swe.remote.keeper``
- :mod:`.ipc`: the Unix socket a hub talks to it over
- :mod:`.discovery`: ``keeper.json``, the lock, finding and starting keepers

``docs/specs/G5-keeper-process.md`` has the design. Nothing here imports
the hub: a keeper is one process per session and stays small.
"""
