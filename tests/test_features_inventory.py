"""FEATURES.md keeps the inventory format QA bots map their checks to.

The file is ``# Features``, one line on what the project is, then
``## <Area>`` sections of one line per feature per surface::

    - **<Name>**: <what the user can do>. Status: stable|beta|planned.
      Surface: CLI|web|iOS|macOS|API|job. Entry point: <...>. Tests: <...>.

(one physical line). Tests is ``none`` or backticked repo paths, which
must exist. AGENTS.md, "Keeping the clients in step", has the rules.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FEATURES = ROOT / "FEATURES.md"
README = ROOT / "README.md"

STATUSES = {"stable", "beta", "planned"}
SURFACES = {"CLI", "web", "iOS", "macOS", "API", "job"}

_FEATURE = re.compile(
    r"^- \*\*(?P<name>[^*]+)\*\*: (?P<desc>.+?\.)"
    r" Status: (?P<status>\S+)\."
    r" Surface: (?P<surface>\S+)\."
    r" Entry point: (?P<entry>.+?)\."
    r" Tests: (?P<tests>.+?)\.$"
)
_PATH = re.compile(r"`([^`]+)`")
_GUESS = "guessing: "


def _areas() -> list[tuple[str, list[re.Match]]]:
    """``(area, feature matches)`` in file order; a malformed line is a
    ``str`` in the list so the shape test can name it."""
    areas: list[tuple[str, list]] = []
    for line in FEATURES.read_text(encoding="utf-8").splitlines()[2:]:
        if not line.strip():
            continue
        if line.startswith("## "):
            areas.append((line[3:].strip(), []))
        elif areas:
            areas[-1][1].append(_FEATURE.match(line) or line)
        else:
            areas.append(("", [line]))
    return areas


def _features() -> list[re.Match]:
    return [m for _, rows in _areas() for m in rows if isinstance(m, re.Match)]


# The Nix derivation builds from src/, tests/ and scripts/ only.
@unittest.skipUnless(FEATURES.exists(), "FEATURES.md is not in this source tree")
class FeaturesInventoryTest(unittest.TestCase):
    def test_header_is_the_title_then_one_line(self):
        lines = FEATURES.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "# Features")
        self.assertTrue(lines[1].strip(), "line 2 is the one line on what the project is")
        self.assertFalse(lines[1].startswith(("#", "-", "|")), lines[1])
        following = next(line for line in lines[2:] if line.strip())
        self.assertTrue(following.startswith("## "), "an area follows the one-line summary")

    def test_every_line_is_an_area_or_a_feature(self):
        bad = [(area, row) for area, rows in _areas() for row in rows
               if isinstance(row, str)]
        self.assertEqual(
            bad, [],
            "these lines are not `- **Name**: what. Status: …. Surface: …. "
            "Entry point: …. Tests: ….` (see AGENTS.md, Keeping the clients in step)",
        )

    def test_every_area_lists_features(self):
        empty = [area for area, rows in _areas() if not rows]
        self.assertEqual(empty, [])
        names = [area for area, _ in _areas()]
        self.assertEqual(len(names), len(set(names)), "an area appears twice")

    def test_status_and_surface_use_the_fixed_words(self):
        bad = [(m["name"], m["status"], m["surface"]) for m in _features()
               if m["status"] not in STATUSES or m["surface"] not in SURFACES]
        self.assertEqual(bad, [], f"Status is one of {sorted(STATUSES)}, Surface one of {sorted(SURFACES)}")

    def test_one_line_per_feature_and_surface(self):
        seen, dupes = set(), []
        for m in _features():
            key = (m["name"], m["surface"])
            if key in seen:
                dupes.append(key)
            seen.add(key)
        self.assertEqual(dupes, [])

    def test_tests_name_real_paths_or_none(self):
        bad = []
        for m in _features():
            tests = m["tests"].removeprefix(_GUESS)
            if tests == "none":
                continue
            paths = _PATH.findall(tests)
            if not paths or _PATH.sub("", tests).replace(",", "").strip():
                bad.append((m["name"], m["tests"]))
            bad += [(m["name"], p) for p in paths if not (ROOT / p).is_file()]
        self.assertEqual(bad, [], "Tests is `none` or backticked paths to existing test files")

    def test_shipped_features_name_an_entry_point(self):
        bad = [m["name"] for m in _features()
               if m["status"] != "planned" and m["entry"].strip().lower() in {"", "none", "none yet"}]
        self.assertEqual(bad, [])


@unittest.skipUnless(FEATURES.exists() and README.exists(), "docs are not in this source tree")
class ReadmeLinksFeaturesTest(unittest.TestCase):
    def test_readme_links_features(self):
        self.assertIn("](FEATURES.md)", README.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
