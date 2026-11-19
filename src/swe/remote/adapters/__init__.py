"""Harness session adapters: one structured channel per harness.

- ``base``: the ``SessionAdapter`` protocol, its dataclasses and errors.
- ``events``: the canonical v2 event model every adapter emits.
- ``fallback``: ``SESSION_FALLBACK``, the read-only floor under
  harness.json's ``capabilities.session``.
- ``registry``: ``adapter_for`` (the only reader of either) and the
  adapter-class registration.
- ``fake``: a scripted adapter for tests.

``remote/protocol_compat.py`` turns v2 events into today's v1 frames.
Contract and event reference: ``docs/specs/G3-adapter-protocol.md``.
"""
