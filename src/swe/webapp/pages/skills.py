"""The Skills page: ``swe skills --json`` built in process, less the skills
that ship inside a plugin (the Plugins page lists those), and one skill's
detail: its frontmatter, its SKILL.md and its folder's files.

Writes: Link to shared skills, Unlink and Link all, per harness skills
folder, as the Mac app's Harness links popover offers them, and New skill
(``post`` below). Frontmatter edits stay in ``swe skills`` and the Mac
app; see docs/web-app/skills.md.

``GET /v1/app/skills`` is the list. ``?skill=<path>`` is one skill's
detail, and ``?skill=<path>&file=<name>`` one file inside its folder. The
skill must be one the inventory lists, and the file must stay inside the
skill's folder once symlinks resolve, so no request reads anything else.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from swe.webapp.pages import CONFIG_LOCK, PageError, home_path

# PrimitiveTree.isTextFile: under a megabyte, and UTF-8 with no NUL in
# its first 8 KB.
MAX_TEXT = 1_000_000
_HEAD = 8192
# PrimitiveTree's section folders, in the order the detail lists them.
_KINDS = (("skills", "Skills"), ("commands", "Commands"), ("agents", "Agents"), ("hooks", "Hooks"))


def get(ctx, args: dict) -> dict:
    home = home_path(ctx)
    inventory = _inventory(home)
    if args.get("skill"):
        skill = _find(inventory, args["skill"])
        if args.get("file"):
            return file_view(skill, args["file"])
        return {**detail(skill, home), "machine": ctx.machine.get("name"),
                "shared_dir": inventory["shared_dir"], "harnesses": inventory["harnesses"]}
    return listing(ctx, home, inventory)


def listing(ctx, home: Path, inventory: dict) -> dict:
    """The list payload, ``GET /v1/app/skills`` with no arguments."""
    skills, in_plugins, catalog_error = placement(inventory["skills"], home)
    return {
        "machine": ctx.machine.get("name"),
        "home": str(home),
        "shared_dir": inventory["shared_dir"],
        "harnesses": inventory["harnesses"],
        "skills": skills,
        "plugin_skill_count": in_plugins,
        "catalog_error": catalog_error,
    }


def _inventory(home: Path) -> dict:
    from swe.skills.inventory import installed_skills

    # The Mac app's swe runs from "/", so no project's .cursor/skills is
    # read; the hub does the same whatever folder it was started in.
    return installed_skills(home=home, cwd=Path(os.sep))


def _find(inventory: dict, path: str) -> dict:
    """The inventory's skill at ``path``. The inventory lists each skill by
    its realpath, so a path reaching that folder through a symlink (a home
    under /var on macOS, say) names it too; only listed skills match."""
    try:
        real = _real(path)
    except (OSError, ValueError):
        real = path
    for skill in inventory["skills"]:
        if skill["path"] in (path, real):
            return skill
    raise PageError(404, "unknown_skill", "No skill at that path on the hub.")


def placement(skills: list[dict], home: Path) -> tuple[list[dict], int, str | None]:
    """SkillPlacement: every skill, each with ``plugin`` set to the ref of
    the plugin it moves to on the Plugins page (else None), how many moved,
    and why the catalog was unreadable (every skill then stays here, so
    none is lost). The page hides the moved ones only while the web app
    has a Plugins page to show them on.

    A skill moves only when its source is ``plugin`` and exactly one
    catalog plugin owns the deepest folder holding it, as
    ``swe.plugins.catalog.owners`` decides.
    """
    try:
        from swe.plugins.catalog import owners, plugin_dirs
        from swe.webapp.pages.plugins import shared_catalog

        # The Plugins page's read: walking every marketplace per Skills
        # load cost seconds on a machine with many plugins.
        dirs = plugin_dirs(shared_catalog(home))
    except Exception as exc:  # the Mac app's CatalogLookup.unavailable
        return [{**s, "plugin": None} for s in skills], 0, str(exc) or exc.__class__.__name__
    placed = []
    for s in skills:
        refs = owners(dirs, s["path"]) if s["source"] == "plugin" else []
        placed.append({**s, "plugin": refs[0] if len(refs) == 1 else None})
    return placed, sum(1 for s in placed if s["plugin"]), None


# -- one skill ---------------------------------------------------------------


def _real(path: Path | str) -> str:
    return os.path.realpath(path)


def _inside(path: Path | str, root: Path | str) -> bool:
    """PrimitiveOwnership.isInside: ``path`` stays under ``root`` once
    symlinks resolve on both."""
    inside = _real(root).rstrip(os.sep)
    return _real(path).startswith(inside + os.sep)


def _contained(folder: Path | str, root: Path | str) -> bool:
    """``folder`` is ``root`` or inside it once symlinks resolve."""
    return _real(folder) == _real(root) or _inside(folder, root)


def editable(skill_path: str, home: Path) -> bool:
    """PrimitiveOwnership.isEditable(skillPath:): under ~/.swe after
    symlinks resolve, and not in its cache. The web app edits nothing; this
    only picks the lock note's words."""
    swe = _real(home / ".swe")
    resolved = _real(skill_path)
    return resolved.startswith(swe + os.sep) and not resolved.startswith(swe + os.sep + "cache" + os.sep)


def frontmatter(text: str) -> list[dict] | None:
    """MarkdownFrontmatter's fields: each key between the opening and
    closing ``---`` whose value is a string it can show: a ``key: value``
    line with nothing indented under it (the value as written), a block
    scalar (``|`` or ``>``, chomping and an indentation digit) or a plain
    value carried over indented lines, both read as YAML reads them. Lists,
    nested maps and anything else under a key are left out. None when the
    file has no frontmatter. CRLF lines read like LF ones."""
    lines = text.split("\n")
    if not text.startswith("---") or len(lines) < 2 or lines[0].strip() != "---":
        return None
    try:
        close = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None
    inside = [line[:-1] if line.endswith("\r") else line for line in lines[1:close]]
    fields = []
    i = 0
    while i < len(inside):
        parsed = _field(inside[i])
        if parsed is None:
            i += 1
            continue
        key, raw = parsed
        # What hangs off the key: indented lines, and blank lines between them.
        end = j = i + 1
        while j < len(inside) and (_blank(inside[j]) or inside[j][:1] in (" ", "\t")):
            if not _blank(inside[j]):
                end = j + 1
            j += 1
        header = _BLOCK_HEADER.match(raw)
        if header:
            value, content_end, consumed = _block(inside, i + 1, header)
            # An indented line the block did not take is YAML the form can't
            # read. The block's own rules decide its extent: a line of only
            # spaces deeper than its indentation is its content, though blank
            # to the scan above.
            if content_end >= end:
                fields.append({"key": key, "value": value})
                i = consumed
                continue
        elif end == i + 1:
            fields.append({"key": key, "value": raw})
        else:
            value = _plain(raw, inside[i + 1:end])
            if value is not None:
                fields.append({"key": key, "value": value})
        i = end
    return fields


# `|` or `>`, at most one chomping indicator and one indentation digit in
# either order, then an optional comment (MarkdownFrontmatter.BlockHeader).
_BLOCK_HEADER = re.compile(r"^([|>])(?:([-+])([1-9])?|([1-9])([-+])?)?(?:[ \t]+#.*)?$")
_NOT_PLAIN_START = set("\"'[]{}&*!|>%@`#,")


def _field(line: str) -> tuple[str, str] | None:
    key, colon, value = line.partition(":")
    if not colon or not key or not all(ch.isalnum() or ch in "-_" for ch in key):
        return None
    if value.startswith(" "):
        value = value[1:]
    return key, value.strip(" \t")


def _blank(line: str) -> bool:
    return line.strip(" \t") == ""


def _spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_break(line: str, indent: int) -> bool:
    return set(line) <= {" "} and len(line) <= indent


def _is_content(line: str, indent: int) -> bool:
    return _spaces(line) >= indent and not _is_break(line, indent)


def _block(lines: list[str], start: int, header: re.Match) -> tuple[str, int, int]:
    """The block scalar at ``start`` as PyYAML and libyaml read one under a
    top-level key: its value, the line after its last content line, and the
    line after the empty lines a ``+`` block keeps."""
    folded = header.group(1) == ">"
    chomp = header.group(2) or header.group(5) or ""
    explicit = header.group(3) or header.group(4)
    k, breaks = start, 0
    if explicit:
        indent = int(explicit)
        while k < len(lines) and _is_break(lines[k], indent):
            breaks, k = breaks + 1, k + 1
    else:
        deepest = 0
        while k < len(lines) and set(lines[k]) <= {" "}:
            deepest, breaks, k = max(deepest, len(lines[k])), breaks + 1, k + 1
        if k < len(lines):
            deepest = max(deepest, _spaces(lines[k]))
        indent = max(1, deepest)
    value, line_break, content_end = "", "", start
    while k < len(lines) and _is_content(lines[k], indent):
        value += "\n" * breaks
        text = lines[k][indent:]
        leading_non_space = text[:1] not in (" ", "\t")
        value += text
        line_break = "\n"
        k += 1
        content_end, breaks = k, 0
        while k < len(lines) and _is_break(lines[k], indent):
            breaks, k = breaks + 1, k + 1
        if not (k < len(lines) and _is_content(lines[k], indent)):
            break
        if folded and leading_non_space and lines[k][indent:][:1] not in (" ", "\t"):
            if breaks == 0:
                value += " "
        else:
            value += line_break
    if chomp == "":
        value += line_break
    elif chomp == "+":
        value += line_break + "\n" * breaks
    return value, content_end, k if chomp == "+" else content_end


def _starts_plain(s: str) -> bool:
    if not s or s[0] in _NOT_PLAIN_START or s in ("-", "?", ":"):
        return False
    return not s.startswith(("- ", "? ", ": "))


def _continues(s: str) -> bool:
    """A list item, a key or a comment on a line of its own is not more of
    the value, whatever YAML would make of it."""
    return (not s.startswith("#") and ": " not in s and " #" not in s and not s.endswith(":")
            and "\t#" not in s and not s.startswith("- ") and s != "-")


def _plain(raw: str, rest: list[str]) -> str | None:
    """A plain value over indented lines, folded as YAML folds it; None for
    anything that reads as a list, a map, a comment or another value."""
    if raw and not (_starts_plain(raw) and _continues(raw) and not re.search(r"[ \t]+#", raw)):
        return None
    value, blanks = raw, 0
    for line in rest:
        if _blank(line):
            blanks += 1
            continue
        # Tabs never indent YAML.
        if not line.startswith(" "):
            return None
        text = line.strip(" \t")
        if not _continues(text):
            return None
        if not value:
            if not _starts_plain(text):
                return None
            value = text
        else:
            value += " " + text if blanks == 0 else "\n" * blanks + text
        blanks = 0
    return value or None


def read_text(path: Path) -> str | None:
    """The file's text when it can preview (PrimitiveTree.isTextFile), else
    None. Undecodable bytes past the first 8 KB read as replacement marks."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_TEXT:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    head = data[:_HEAD]
    if b"\0" in head:
        return None
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A character cut at the 8 KB mark is not a binary file.
        if len(data) <= _HEAD or exc.start < len(head) - 3:
            return None
    return data.decode("utf-8", errors="replace")


def _entries(folder: Path, root: Path, prefix: str = "", depth: int = 0, limit: int = 6,
             skip=()) -> list[dict]:
    """Every file under ``folder``, names relative to it, dotfiles skipped
    at every level; ``skip`` names top-level folders left to a section. A
    folder whose symlinks lead out of ``root`` is not listed, so no name
    from elsewhere on the hub reaches the page."""
    if depth > limit or not _contained(folder, root):
        return []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    for name in names:
        if name.startswith("."):
            continue
        item = folder / name
        if item.is_dir():
            if depth == 0 and name in skip:
                continue
            out += _entries(item, root, prefix + name + "/", depth + 1, limit)
        else:
            out.append({"name": prefix + name, "folder": False})
    return out


def tree(root: Path) -> list[dict]:
    """PrimitiveTree: a skill's own SKILL.md first as its Main file, then
    files under Skills, Commands, Agents, Hooks and Other files, each name
    relative to ``root``. A ``skills/<name>`` folder
    is one entry that opens its SKILL.md, with its files as children."""
    sections = []
    for kind, title in _KINDS:
        folder = root / kind
        if not folder.is_dir() or not _contained(folder, root):
            continue
        entries = []
        try:
            top = sorted(os.listdir(folder))
        except OSError:
            top = []
        for name in top:
            if name.startswith("."):
                continue
            item = folder / name
            if item.is_dir() and not _contained(item, root):
                continue
            if kind == "skills" and item.is_dir():
                entries.append({
                    "name": name, "folder": True, "path": f"{kind}/{name}",
                    "preview": f"{kind}/{name}/SKILL.md",
                    "children": [{"name": e["name"], "path": f"{kind}/{name}/{e['name']}"}
                                 for e in _entries(item, root)],
                })
            elif item.is_dir():
                try:
                    inner = sorted(os.listdir(item))
                except OSError:
                    inner = []
                for leaf in inner:
                    if not leaf.startswith(".") and not (item / leaf).is_dir():
                        rel = f"{name}/{leaf}"
                        entries.append({"name": rel, "folder": False, "path": f"{kind}/{rel}"})
            else:
                entries.append({"name": name, "folder": False, "path": f"{kind}/{name}"})
        if entries:
            sections.append({"kind": kind, "title": title, "entries": entries})
    other = [{"name": e["name"], "folder": False, "path": e["name"]}
             for e in _entries(root, root, limit=4, skip={k for k, _ in _KINDS})]
    # A skill folder's own SKILL.md is the skill, not a stray file.
    main = [e for e in other if e["name"] == "SKILL.md"]
    if main:
        other.remove(main[0])
        sections.insert(0, {"kind": "main", "title": "Main file", "entries": main})
    if other:
        sections.append({"kind": "other", "title": "Other files", "entries": other})
    return sections


def detail(skill: dict, home: Path) -> dict:
    root = Path(skill["path"])
    skill_md = root / "SKILL.md"
    text = read_text(skill_md) if _inside(skill_md, root) else None
    from swe.skills.usage import skill_usage
    return {
        "skill": skill,
        "home": str(home),
        "editable": editable(skill["path"], home),
        "skill_file": str(skill_md) if skill_md.is_file() else None,
        "skill_md": text,
        "frontmatter": frontmatter(text) if text is not None else None,
        "tree": tree(root),
        "usage": skill_usage(skill["name"], home),
    }


def file_view(skill: dict, name: str) -> dict:
    """One file of the skill's folder, by its name relative to the folder.
    ``text`` is None when the file can't preview: binary, over a megabyte,
    or resolving outside the folder."""
    root = Path(skill["path"])
    if not name or name.startswith("/") or "\0" in name:
        raise PageError(400, "bad_file", "Give a file name inside the skill's folder.")
    target = root / name
    outside = not _inside(target, root)
    if not outside and not target.is_file():
        raise PageError(404, "unknown_file", "No such file in the skill's folder.")
    return {
        "name": name,
        "path": str(target),
        "outside": outside,
        "text": None if outside else read_text(target),
    }


# -- writes: POST /v1/app/skills/<action> --------------------------------------
#
# swe links whole skills folders, not single skills, so every write
# names a harness skills folder from the inventory's ``harnesses``:
#
# - ``link`` ``{"harness", "expect"?, "skill"?}``: point the folder at
#   ~/.swe/skills, as the Mac app's "Link to shared skills" does
#   (``swe skills link <path> [--force] --json``). Only a Not linked folder
#   (create, relink, absorb) is linked; ``--force`` is passed only for an
#   absorb folder (copies of shared skills, or a tree with no file in it),
#   which link_skill_root backs up first. keep, conflict, ignored and skipped are refused with
#   the reason, so a folder holding the only copy of a skill is never
#   replaced. An archived folder can be linked here, as in the app.
# - ``unlink`` ``{"harness", "expect"?, "skill"?}``: remove a Linked
#   folder's symlink (``swe skills unlink <path> --json``).
# - ``link_all`` ``{"harnesses"?, "skill"?}``: link every folder
#   ``link_all_targets`` clears (never archived, never one needing
#   ``--force``), one at a time; a failure is reported and the rest go on.
#   ``harnesses`` limits it to the folders the page's confirm named.
#
# ``harness`` is a folder's path (a name works when one folder has it).
# ``expect`` is the state the page showed and ``expect_detail`` its detail
# (an empty folder that fills with shared copies stays ``absorb`` but now
# needs a backup): when the folder has changed since, nothing is written
# and the refusal says what it is now. ``skill``
# is the skill whose detail is open; the reply carries its row again.
#
# The reply is the list payload (``listing``) read after the write, plus
# ``action``, ``harness`` (the folder's new row), ``result`` (the
# LinkReply/UnlinkReply ``--json`` shape, or for link_all
# ``{"linked", "failed", "skipped"}``) and ``skill``.
#
# - ``new`` ``{"name", "description"?}``: ``swe skills new <name>
#   [--description=...]`` (``skills.create.create``), which writes
#   ``~/.swe/skills/<name>/SKILL.md``. A name or description it refuses
#   is a 422 ``bad_skill`` with its reason. The reply is the list payload
#   plus ``action`` and ``created``: the new SKILL.md's path and its row
#   (``skill``) when the inventory lists it.

ACTIONS = ("link", "unlink", "link_all", "toggle", "new")
TEXT_MAX = 2048
REF_MAX = 4096

# The hub answers each request on its own thread, and every link rewrites
# skill_links.json; the Mac app runs its swe calls one at a time for the
# same reason (SkillsModel's SerialGate). Reading the states, checking and
# writing all happen under it. Shared with MCP and Setup, whose apply
# relinks skills too.
_WRITE_LOCK = CONFIG_LOCK


def link_state(root: dict) -> str:
    """SkillRoot.link: linked, selection, notLinked or blocked.

    A selection root links some skills and not others, so it is none of
    the whole-folder answers: bulk "link all" must leave it be (it is not
    a loose folder), while a single "Link" or "Unlink" on it stays legal —
    Link re-ties it as a whole-folder link, Unlink drops its links.
    """
    if root["state"] == "linked":
        return "linked"
    if root["state"] == "selection":
        return "selection"
    return "notLinked" if root["state"] in ("create", "relink", "absorb") else "blocked"


def needs_force(root: dict) -> bool:
    """SkillRoot.linkNeedsForce: a folder holding copies of shared skills."""
    return root["state"] == "absorb" and root.get("detail") != "empty"


def explanation(root: dict) -> str:
    """SkillRoot.explanation: one sentence on why the folder is in its state."""
    state, detail, archived = root["state"], root.get("detail") or "", root.get("archived")
    if state == "linked":
        return ("Reads the shared skills. Archived in swe, so swe init won't relink it if you unlink it, but you can link it here."
                if archived else "Reads the shared skills in ~/.swe/skills.")
    if state == "create":
        return ("No skills folder yet. Archived in swe, so swe init leaves it alone; linking here creates it."
                if archived else "No skills folder yet. Linking creates it.")
    if state == "relink":
        return f"Points somewhere else: {detail.replace('points at ', '')}." if detail else "Points somewhere else."
    if state == "absorb":
        return ("An empty folder. Linking replaces it." if detail == "empty"
                else f"Holds only copies of shared skills ({detail}). Linking backs it up and replaces it.")
    if state == "keep":
        return f"Holds skills that exist nowhere else ({detail}). Move them into ~/.swe/skills before linking."
    if state == "ignored":
        return f"Ignored: {detail}." if detail else "Ignored by swe init."
    if state == "selection":
        n_off = len(root.get("disabled") or [])
        picks = {
            "except": "links every shared skill but the ones switched off",
            "only": "links just the shared skills switched on",
        }.get(root.get("mode"), "links picked shared skills")
        off = f" ({n_off} off)" if n_off else ""
        return f"A selection{off}: it {picks}."
    if state == "skipped":
        return "The harness isn't installed."
    return f"{state}: {detail}" if detail else state


def link_all_targets(roots: list[dict]) -> tuple[list[dict], list[dict]]:
    """SkillsModel.linkAllTargets: the loose folders that cost nothing to
    point at the shared root, and the ones a person should look at."""
    link, skipped = [], []
    for root in roots:
        state, archived = link_state(root), bool(root.get("archived"))
        if state == "notLinked" and not archived and not needs_force(root):
            link.append(root)
        elif state == "notLinked" and not archived:
            skipped.append(root)
        elif state == "blocked" and root["state"] in ("keep", "conflict"):
            skipped.append(root)
    return link, skipped


def post(ctx, action: str, body: dict) -> dict:
    if action not in ACTIONS:
        raise PageError(404, "unknown_action", f"unknown skills action: {action}")
    if not isinstance(body, dict):
        raise PageError(422, "bad_body", "Send a JSON object.")
    home = home_path(ctx)
    if action == "new":
        return _new(ctx, home, body)
    skill_ref = _ref(body, "skill", required=False)
    with _WRITE_LOCK:
        inventory = _inventory(home)
        if skill_ref is not None:
            skill_ref = _find(inventory, skill_ref)["path"]
        if action == "link_all":
            root = None
            result = _link_all(inventory["harnesses"], home, _named(body))
        elif action == "toggle":
            root = _root(inventory["harnesses"], _ref(body, "harness"))
            _expect(root, body.get("expect"), body.get("expect_detail"))
            skill_row = _find(inventory, _ref(body, "skill"))
            skill_ref = skill_row["path"]
            result = _toggle(skill_row, root, body.get("enabled"), home)
        else:
            root = _root(inventory["harnesses"], _ref(body, "harness"))
            _expect(root, body.get("expect"), body.get("expect_detail"))
            result = _link(root, home) if action == "link" else _unlink(root, home)
        after = _inventory(home)
    reply = listing(ctx, home, after)
    reply.update({
        "action": action,
        "harness": next((r for r in after["harnesses"] if root and r["path"] == root["path"]), None),
        "result": result,
        "skill": next((s for s in after["skills"] if s["path"] == skill_ref), None),
    })
    return reply


def _new(ctx, home: Path, body: dict) -> dict:
    from swe.skills.create import create

    name, description = body.get("name"), body.get("description")
    if not isinstance(name, str) or len(name) > TEXT_MAX:
        raise PageError(422, "bad_skill", "name must be the new skill's name")
    if description is not None and (not isinstance(description, str) or len(description) > TEXT_MAX):
        raise PageError(422, "bad_skill", "description must be one line of text")
    with _WRITE_LOCK:
        try:
            target, _ = create(name.strip(), (description or "").strip() or None, home)
        except (ValueError, OSError) as exc:
            raise PageError(422, "bad_skill", str(exc)) from exc
        # Same follow-up as `swe skills new`: the new shared skill links
        # into every except selection right away, not on the next sync.
        from swe.skills.link_ops import reconcile_all_selections
        linked_into = [row["label"] for row in reconcile_all_selections(home)]
        after = _inventory(home)
    # The inventory lists real paths (macOS's /var is /private/var).
    mine = {_real(target), _real(target.parent)}
    reply = listing(ctx, home, after)
    reply.update({
        "action": "new",
        "created": str(target),
        "linked_into": linked_into,
        "skill": next((s for s in after["skills"] if _real(s["path"]) in mine), None),
    })
    return reply


def _ref(body: dict, key: str, required: bool = True) -> str | None:
    value = body.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > REF_MAX or "\0" in value:
        raise PageError(422, f"bad_{key}", f"{key} must be a {key} the page lists.")
    return value


def _named(body: dict) -> set[str] | None:
    value = body.get("harnesses")
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) > 200
            or not all(isinstance(v, str) and v and len(v) <= REF_MAX for v in value)):
        raise PageError(422, "bad_harnesses", "harnesses must be the folder paths the page lists.")
    return set(value)


def _root(roots: list[dict], ref: str) -> dict:
    """The harness skills folder ``ref`` names: its path, or a name only
    one folder has. Nothing outside the inventory's list is accepted."""
    for root in roots:
        if root["path"] == ref:
            return root
    named = [r for r in roots if r["name"] == ref]
    if len(named) == 1:
        return named[0]
    if named:
        raise PageError(422, "ambiguous_harness", f"{ref} has {len(named)} skills folders; name one by its path.")
    raise PageError(404, "unknown_harness", "No harness skills folder like that on the hub.")


def _expect(root: dict, expect, detail) -> None:
    if expect is not None and not isinstance(expect, str):
        raise PageError(422, "bad_expect", "expect must be the state the page showed.")
    if detail is not None and not isinstance(detail, str):
        raise PageError(422, "bad_expect", "expect_detail must be the detail the page showed.")
    if ((expect is not None and expect != root["state"])
            or (detail is not None and detail != (root.get("detail") or ""))):
        raise PageError(409, "state_changed",
                        f"{root['name']}'s skills folder changed since the page read it. "
                        f"{explanation(root)} Look again before you change it.")


def _same_folder(root: dict, home: Path) -> None:
    """swe resolves a folder path to a root of its own; refuse unless it
    lands on the very folder the inventory listed."""
    from swe.skills.layout import SkillLayoutError, resolve_root_ref

    try:
        _label, path = resolve_root_ref(root["path"], home=home, cwd=Path(os.sep))
    except SkillLayoutError as exc:
        raise PageError(409, "link_failed", str(exc)) from exc
    if str(path) != root["path"]:
        raise PageError(409, "link_failed", f"swe reads {root['path']} as {path}; change it with swe skills.")


def _link(root: dict, home: Path) -> dict:
    from swe.configuration import ConfigurationError
    from swe.skills.link_ops import SkillLayoutError, link_skill_root
    from swe.skills.models import LinkReply

    state = link_state(root)
    if state == "linked":
        raise PageError(409, "already_linked", f"{root['name']} already reads the shared skills.")
    if state == "blocked":
        # keep marks the only copy of something; conflict is a file in the
        # way. Neither is ever replaced from here.
        raise PageError(409, root["state"], f"SWE won't link {root['name']}. {explanation(root)}")
    _same_folder(root, home)
    # Every absorb folder goes with --force, the "empty" one too: init calls
    # a tree of empty subfolders empty, but link_skill_root refuses any
    # folder with an entry in it unless forced. Both kinds were just
    # classified (no file, or only copies of shared skills), and --force
    # backs the folder up before it is removed, so nothing can be lost.
    # A selection holding private folders asks the same --force; one that
    # is all links is relinked without it.
    force = root["state"] == "absorb"
    try:
        label, src, tgt = link_skill_root(root["path"], home=home, cwd=Path(os.sep), force=force)
    except (SkillLayoutError, ConfigurationError) as exc:
        raise PageError(409, "link_failed", str(exc)) from exc
    return LinkReply(label, str(src), str(tgt)).to_json()


def _unlink(root: dict, home: Path) -> dict:
    from swe.configuration import ConfigurationError
    from swe.skills.link_ops import SkillLayoutError, unlink_skill_root
    from swe.skills.models import UnlinkReply

    if root["state"] not in ("linked", "selection"):
        raise PageError(409, "not_linked", f"{root['name']}'s folder isn't linked to the shared skills.")
    _same_folder(root, home)
    try:
        label, path = unlink_skill_root(root["path"], home=home, cwd=Path(os.sep))
    except (SkillLayoutError, ConfigurationError) as exc:
        raise PageError(409, "unlink_failed", str(exc)) from exc
    return UnlinkReply(label, str(path), False).to_json()


def toggleable(root: dict) -> bool:
    """SkillsModel.chips' ``toggleable``: a folder swe can attach one
    skill's link into. relink points at another tree; ignored, skipped
    and conflict take nothing. Everything else — the whole-folder link,
    a selection, a missing or absorb folder, a keep dir whose private
    skills sit beside the link — toggles."""
    return root["state"] in ("linked", "selection", "create", "absorb", "keep")


def _toggle(skill: dict, root: dict, enabled, home: Path) -> dict:
    """``swe skills enable|disable <name> --harness=<folder>``: the per
    skill switch a detail chip carries, on a shared skill only."""
    from swe.configuration import ConfigurationError
    from swe.skills.link_ops import SkillLayoutError, set_skill_enabled

    if not isinstance(enabled, bool):
        raise PageError(422, "bad_enabled", "enabled must be true or false.")
    if skill.get("source") != "shared":
        raise PageError(409, "not_shared",
                        f"{skill['name']} isn't in the shared library; only shared skills toggle.")
    if not toggleable(root):
        raise PageError(409, root["state"],
                        f"SWE won't toggle skills in {root['name']}. {explanation(root)}")
    _same_folder(root, home)
    try:
        return set_skill_enabled(skill["name"], root["path"], enabled=enabled,
                                 home=home, cwd=Path(os.sep))
    except (SkillLayoutError, ConfigurationError) as exc:
        raise PageError(409, "toggle_failed", str(exc)) from exc


def _link_all(roots: list[dict], home: Path, named: set[str] | None) -> dict:
    """SkillsModel.linkAll: each target in turn; a failure lands in
    ``failed`` and the rest go on. With ``named``, only those folders,
    and one named that is no longer a target is reported, not linked."""
    targets, skipped = link_all_targets(roots)
    failed = []
    if named is not None:
        target_paths = {r["path"] for r in targets}
        # A folder the confirm named that the hub no longer lists at all
        # (its harness left the registry) is reported too, not dropped.
        listed = {r["path"] for r in roots}
        for path in sorted(named - listed):
            failed.append({"name": Path(path).parent.name.lstrip(".") or path, "path": path,
                           "message": "no longer listed on the hub."})
        for root in roots:
            if root["path"] in named and root["path"] not in target_paths:
                failed.append({"name": root["name"], "path": root["path"],
                               "message": f"changed since the page read it. {explanation(root)}"})
        targets = [r for r in targets if r["path"] in named]
    linked = []
    for root in targets:
        try:
            _link(root, home)
        except PageError as exc:
            failed.append({"name": root["name"], "path": root["path"], "message": exc.message})
        except OSError as exc:
            failed.append({"name": root["name"], "path": root["path"], "message": str(exc)})
        else:
            linked.append({"name": root["name"], "path": root["path"]})
    return {"linked": linked, "failed": failed, "skipped": skipped}
