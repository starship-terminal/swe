"""The session view's frame fixtures (tests/fixtures/session-view/) are what
the hub emits now.

``scripts/record_session_view.py`` starts a real hub over a throwaway home
with a keeper driving the scripted fake adapter, opens a v2 stream the way
the React client does and writes down every frame. The React client's
reducer tests replay those files, so a hub change that alters what a stream
sends must change the fixtures in the same commit: this test records every
scenario again and holds the committed files equal to the result, so the two
sides cannot drift. Run ``PYTHONPATH=src:. python3 scripts/record_session_view.py``
to write them again.
"""

import importlib.util
import io
import json
import unittest
from pathlib import Path

from swe.remote import schema

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "record_session_view.py"
FIXTURES = ROOT / "tests" / "fixtures" / "session-view"


def load_recorder():
    spec = importlib.util.spec_from_file_location("record_session_view",
                                                  SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSessionViewFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.recorder = load_recorder()
        report = io.StringIO()
        try:
            cls.recorded = cls.recorder.record_all(report)
        except RuntimeError:
            raise AssertionError(report.getvalue()) from None

    def test_the_committed_files_are_what_the_hub_emits(self):
        committed = sorted(p.name for p in FIXTURES.glob("*.json"))
        self.assertEqual(committed,
                         sorted(f"{n}.json" for n in self.recorded),
                         "a scenario and its file must come and go together")
        for name, fixture in self.recorded.items():
            with self.subTest(name):
                self.assertEqual(
                    (FIXTURES / f"{name}.json").read_text(),
                    self.recorder.render(fixture),
                    f"{name}.json is stale: run "
                    "PYTHONPATH=src:. python3 scripts/record_session_view.py")

    def test_every_frame_and_snapshot_is_valid_against_the_schema(self):
        for path in sorted(FIXTURES.glob("*.json")):
            fixture = json.loads(path.read_text())
            with self.subTest(path.name):
                self.assertTrue(fixture["connections"])
                for conn in fixture["connections"]:
                    self.assertTrue(conn["frames"])
                    for frame in conn["frames"]:
                        self.assertEqual(schema.validate(frame, "Frame"), [],
                                         frame)
                for snap in fixture["snapshots"]:
                    self.assertEqual(schema.validate(snap["body"], "Snapshot"),
                                     [], snap)
                    conn, frame = snap["after"]
                    self.assertLess(frame, len(
                        fixture["connections"][conn]["frames"]))

    def test_nothing_of_this_machine_is_in_a_fixture(self):
        for path in sorted(FIXTURES.glob("*.json")):
            text = path.read_text()
            for needle in ("/Users/", "/var/folders", "/tmp/", "/home/",
                           "kaichen"):
                if needle == "/home/":
                    # The one made-up home the recorder writes.
                    text = text.replace("/home/user", "")
                self.assertNotIn(needle, text, path.name)

    def test_the_recording_is_deterministic(self):
        again = self.recorder.record_all(io.StringIO())
        self.assertEqual(
            {n: self.recorder.render(f) for n, f in again.items()},
            {n: self.recorder.render(f) for n, f in self.recorded.items()})


if __name__ == "__main__":
    unittest.main()
