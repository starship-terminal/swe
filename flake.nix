{
  description = "swe: one command to launch, resume, and analyze many AI coding agents";

  # Pinned to the same release branch the consumer dotfiles use, so a
  # `follows` there dedupes the package set instead of silently building
  # against a different Python.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";

  outputs = { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      forAllSystems = f: lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in
    {
      packages = forAllSystems (pkgs: rec {
        default = swe;

        swe = pkgs.python3Packages.buildPythonApplication {
          pname = "swe";
          # One source of truth: the wheel and the derivation cannot drift.
          version = (lib.importTOML ./pyproject.toml).project.version;
          pyproject = true;

          # Only these paths affect the build hash. Editing docs, examples or
          # CHANGELOG.md does not trigger a rebuild.
          src = lib.fileset.toSource {
            root = ./.;
            fileset = lib.fileset.unions [
              ./pyproject.toml
              ./README.md   # pyproject declares readme = "README.md"
              ./src
              ./tests
              # tests/test_issue_labels.py and tests/test_bug.py read the
              # issue forms and the labeller out of the repo root; without
              # these the sandbox checkPhase dies on FileNotFoundError.
              ./scripts
              ./.github/ISSUE_TEMPLATE
              # The suite also reads the clients it must stay in step
              # with: mac scripts and SWECore Swift sources (setup-machine,
              # doctor, plan/provider parity tests) and the generated
              # landing pages (docs_site). app/ios is app-only.
              ./app/mac
              ./app/landing-page
              # tests/test_harness_docs.py checks this page and example
              # against the code and reads both at import time. Only these
              # two files, so other doc edits still leave the hash alone.
              ./docs/harness-support.md
              ./examples/harness.example.json
            ];
          };

          build-system = with pkgs.python3Packages; [ hatchling ];

          # The core CLI is stdlib-only, with no runtime dependencies at
          # all: tomllib is stdlib from 3.11, which is the floor.
          dependencies = [ ];

          # The suite runs under unittest, not pytest: tests/conftest.py is a
          # pytest-only fixture and is never imported here. Every command
          # reads ~/.swe, so the check gets a throwaway HOME like CI does.
          # One test inits a git repo to find a project root.
          nativeCheckInputs = [ pkgs.git ];
          checkPhase = ''
            runHook preCheck
            HOME="$(mktemp -d)" python -m unittest discover -s tests -p 'test_*.py'
            runHook postCheck
          '';
          pythonImportsCheck = [ "swe" ];

          meta = {
            description = "One command to launch, resume, and analyze many AI coding agents";
            homepage = "https://github.com/starship-terminal/swe";
            license = lib.licenses.mit;
            mainProgram = "swe";
            platforms = lib.platforms.unix;
          };
        };
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };
      });

      # `nix flake check` builds the package, which runs the test suite.
      checks = forAllSystems (pkgs: {
        default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.hatchling ps.coverage ]))
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
          '';
        };
      });
    };
}
