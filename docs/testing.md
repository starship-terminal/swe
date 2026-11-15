# Testing

How to run each suite, the rules that keep tests off real user data, and
how coverage is measured. For what a module is supposed to do, see
[ARCHITECTURE.md](../ARCHITECTURE.md); for the dated coverage audit see
[COVERAGE.md](COVERAGE.md).

## The throwaway-HOME rule

Everything `swe` does is rooted at `~/.swe`, so the suite must never run
against a real home directory. Every test run uses a fresh `HOME`:

```bash
test_home="$(mktemp -d)"
HOME="$test_home" python3 -m unittest discover -s tests -p 'test_*.py'
```

Tests that need a home build one themselves with `tempfile` plus the
`*_for(home)` path helpers in `swe/paths.py`. A test that would read or
write the real `~/.swe`, `~/.claude`, `~/.codex` or any other harness
directory is a bug; do not add one.

## Python

Setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

The suite is `unittest` (pytest is not installed):

```bash
python3 -m unittest discover -s tests -p 'test_*.py'   # the whole suite
python3 -m unittest tests.test_table                    # one file
python3 -m unittest tests.test_table -k render          # one test
```

Always run it against the throwaway `HOME` above. Integration tests launch
`swe` as a subprocess, so they write registries and caches into whatever
`HOME` they get.

Conventions that keep the suite working:

- Session parser tests mock `os.path.expanduser` in
  `swe.sessions.parsers` to redirect `~/.tool/` paths into temp dirs.
- Engine tests use `expand_path()`, which expands only `~` prefixes.
- Completion tests mock `load_registry` for tool/tag completion and
  `SHELL_CONFIGS` for script generation.
- Class-level JSON fixtures are shallow-copied by default. A test that
  mutates nested keys must `copy.deepcopy` first.
- A test's temp folder comes from `temp_dir(self)` in `tests/_tempdir.py` (or `self.enterContext(tempfile.TemporaryDirectory())`), never a bare `tempfile.mkdtemp()`, which leaves the folder behind on every run.
- The syntax floor is Python 3.11: no PEP 701 nested f-strings.
  `tests/test_syntax_floor.py` enforces it.
- Width-asserting tests patch `swe.console.terminal_width`; nothing else
  pins terminal size under unittest.
- A test module's `if __name__ == "__main__": unittest.main()` goes at the
  very end of the file. Anything below it is skipped when the file is run
  directly, even though discovery still finds it. `tests/test_agent_rules.py`
  enforces it.
- The suite also runs inside the Nix build sandbox (`nix build`, the
  `Nix flake build and check` job on every PR). There is no `/usr/bin/env`
  and nothing in `/usr/bin` or `/bin` but `/bin/sh`; Python, git, bash and
  coreutils come from the store and are only on the inherited `PATH`. So a
  stub executable a test writes starts with `#!` plus `sys.executable` (or
  `#!/bin/sh` using only shell builtins), a script is run through
  `shutil.which("bash")`, and a test that trims `PATH` keeps the tools it
  needs. A test that reads a file outside `src/` and `tests/` needs that
  file in the source filter in `flake.nix`.

### Interpreter matrix

Python floor is 3.11 and CI runs the endpoints, 3.11 and 3.13. No version-gated code remains in the tree: `tomllib` is stdlib throughout, so the two legs guard against stdlib drift rather than covering a branch of our own. Do not add a 3.10 compatibility shim back: if a user reports a 3.10 failure, the answer is `v0.2.9`, the last release that runs on 3.10, not a new fork in the source. Ubuntu 22.04 LTS is the distro this actually affects.

### End-to-end checks against the installed `swe`

**Every e2e feature must be verified against the installed `swe` binary, not just unit tests.**

The `swe` command is a pip-installed console entry point (declared in `[project.scripts]` in `pyproject.toml`). New Python files added to `src/swe/` are **not** available to the installed `swe` until you reinstall. Unit tests with `PYTHONPATH=src` can pass while the installed binary silently fails because it doesn't see the new module.

Required steps for any feature that adds files or changes `cmd_*` handlers:

1. **Write tests** and run `HOME="$(mktemp -d)" python3 -m unittest discover -s tests -p 'test_*.py'`.
2. **Reinstall the package** with `pip install -e .` (or `pipx install --force git+...` for pipx installs). With an editable install new files are picked up automatically, but a stale non-editable install won't see them until you reinstall.
3. **Verify e2e** by running the actual `swe <command>` against a temp HOME, not just `PYTHONPATH=src python -m swe.cli <command>`. CI does the same on every PR (the editable install on Linux), and `release.yml` repeats it on a tag for a built wheel in a clean venv on Linux and macOS, the Nix derivation's output and the bundled app, each against a `mktemp -d` home. The native-only checks that used to sit on the PR path (the macOS install smoke test and the app unit tests) run nightly, on merge_group and on demand in `native.yml`; the Nix build runs on every PR in `ci.yml`. Read `.github/workflows/ci.yml` and `release.yml` for the exact commands rather than assuming; that list gets trimmed from time to time.
4. **Open a PR**, one concern per PR, with a clear description of what changed and why.

Common pitfall: if a feature works with `PYTHONPATH=src python -m swe.cli` but not with the installed `swe` command, the installed copy is stale (likely a non-editable install). Re-run `pip install -e .` to switch to editable mode and sync with the source tree.

### Web app tests

`tests/test_webapp_*.py` cover the web app's page data (`/v1/app/*`)
through a real hub on a free port (`HubBase` in `tests/test_remote_hub.py`).
Give that hub `WebApp(home, download_prices=False)` so no test downloads
the LiteLLM price list.

Behaviour in `src/swe/webapp/static/` is tested by running the page
scripts under `node` with the browser stubbed (`STUB` in
`tests/test_webapp_launch_sheets.py` is the complete prelude). Those tests
skip when `node` is not on PATH, so a machine without it silently runs
fewer tests; CI's runner image ships node.

### Python coverage

The same measurement CI makes (see `.github/workflows/ci.yml`). CI splits
the run across two jobs with `scripts/run_test_shard.py` and combines their
data in its Coverage job; locally one run does it all:

```bash
test_home="$(mktemp -d)"
HOME="$test_home" python3 -m coverage run -m unittest discover -s tests -p 'test_*.py'
python3 -m coverage combine
python3 -m coverage report
```

`.coveragerc` enables branch and subprocess tracking (`parallel` plus
`patch = subprocess`), which is why the run must `combine` before
reporting. The regression floor is `fail_under = 84` in `.coveragerc`,
enforced by the coverage job; the dated audit in [COVERAGE.md](COVERAGE.md)
lists the current risk areas.

## Swift

The iPhone app in `app/ios/` has its own XCTest suite, run on a
simulator; [app/ios/README.md](../app/ios/README.md) has the command, and
the nightly `native.yml` runs its unit target. The rest of this section
is the macOS app, whose package has three test targets: `SWECoreTests`
(the logic, and almost every test), `SWEAppUITests` (views and AppKit
glue that need a run loop or a window) and `SWEWidgetsUITests`.

From `app/mac/`:

```bash
swift test                      # everything
swift test --filter Limits      # a name filter
```

Tests use Swift Testing (`@Suite` / `@Test` / `#expect`). Conventions:

- Async waits go through continuation-based gates or `TestClock` (see
  `UsageTestSupport.swift`). Never busy-wait or `sleep` in a test.
- Timing tests use tolerances, not exact comparisons; assert a window or
  an ordering, not a wall-clock value.
- Fixtures that feed `SweClient` and the parsers live under
  `Tests/SWECoreTests/Fixtures/`.
- Temporary folders come from `TempDir` (`LedgerTestSupport.swift`), which
  removes itself; keep it alive (`withExtendedLifetime`) past any read that
  opens a file by path. A test that needs a `UserDefaults` suite takes a
  `TestDefaults` (`TestSupport.swift`), whose plist lives in the test
  process's temp folder. Never `UserDefaults(suiteName:)` with a plain
  name: that writes `~/Library/Preferences`, and neither
  `removePersistentDomain(forName:)` nor deleting the file reliably cleans
  it up. A model whose defaults point at the real home (`APIKeysModel`'s
  shell and rc files) gets a temp one too.
- Fixtures and test data hold made-up figures and names: the repository
  is public.
- A new dependency that sleeps or polls takes an injectable clock or a
  gate so the test can drive it deterministically.
- A test gate remembers a release that lands before its caller parks,
  and every wait is bounded by `gateTimeout`. Bound a wait with
  `withinTimeout`, which never awaits the loser, not a task group: a group
  waits for every child, and `await task.value` ignores cancellation.
- Never execute a stub script the test just wrote. macOS checks a new
  executable before its first run, and on a loaded machine that held a
  stub in `_dyld_start` for minutes. Run it as `/bin/sh <stub>` (a small
  internal `interpreter` parameter on the code that launches it, or a test
  runner that puts the script first in `/bin/sh`'s arguments), or link the
  stub name to a system binary. A child that has to be a real executable
  (the signed impostor in `FanTests`) is waited on with `waitForExit`,
  which kills its tree at `gateTimeout` instead of hanging the suite.

### Capturing swe contract fixtures

The fixtures in `app/mac/Tests/SWECoreTests/Fixtures/` are real `swe`
output, captured once against a throwaway `HOME` seeded with made-up
registry entries (`fixturetool`, `oldtool`), a fixture marketplace and a
fixture session. To refresh or add one:

1. `pip install -e ".[test]"` so the `swe` binary exists.
2. Build a throwaway `HOME` the way the Python tests do (registry entries
   under `$HOME/.swe/config/harness.json`, plus any transcripts or
   plugin folders the command reads).
3. Run `HOME=$test_home swe <command> --json > app/mac/Tests/SWECoreTests/Fixtures/<name>.json`.
4. Where a command needs a live harness CLI or a provider (`--fix`,
   plugin actions, `swe list --usage` rate readings), build the fixture to
   the shape `src/swe` emits instead, and say so in the test file's
   header comment, as `SweContractTests.swift` does.

`SweContractTests` decodes every fixture through the same `SweClient` /
`Decodable` the app uses, so a contract change breaks a test, not the app.

### Swift coverage

```bash
cd app/mac
swift test --enable-code-coverage
xcrun llvm-cov report \
  .build/arm64-apple-macosx/debug/SWEPackageTests.xctest/Contents/MacOS/SWEPackageTests \
  -instr-profile .build/arm64-apple-macosx/debug/codecov/default.profdata
```

Filter the report rows by `Sources/SWECore` or `Sources/SWEWidgetsUI`
for a per-target total. `SWEApp` is mostly views; `SWEAppUITests`
covers the parts with logic worth pinning. There is no Swift coverage
floor in CI, and the macOS unit tests run nightly, not on pull requests,
so run the suites a change touches before pushing. The report is a guide
for where to add tests.
