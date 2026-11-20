"""The session keeper: one process per live SWE-started session.

G4 ships only the keeper's durable event log (:mod:`.log`); the keeper
process, its IPC socket, the flock and discovery are G5's. See
``docs/specs/G4-keeper-event-log.md``.
"""
