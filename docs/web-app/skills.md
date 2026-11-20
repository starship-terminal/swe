# Skills

The Mac app's Skills page (`SkillsPage.swift`, `SkillDetailView` in
`PrimitiveDetail.swift`, `SkillsModel` in `SWECore/Primitives/Skills.swift`)
as a web page. It links and unlinks harness skills folders as the app's
Harness links popover does, and writes a new skill as the app's New skill
sheet does; everything else is read only. Module
`swe/webapp/pages/skills.py`, script `static/pages/skills.js`, styles
`static/pages/skills.css`.

## What it shows

- **List** (`#/skills`): every skill on the hub machine outside a plugin,
  from `swe skills --json` built in process (`skills/inventory.py::installed_skills`).
  A skill moves to the Plugins page when its source is `plugin` and exactly
  one catalog plugin owns the deepest folder holding it
  (`plugins/catalog.py::plugin_dirs` and `owners`, the rule the app's
  `SkillPlacement` follows), but only once the web app has a Plugins page
  to show them on; until then every skill is listed here. If the catalog
  can't be read, every skill stays and a caption says why. Harness and Source selects (only values the list
  has), "Filter skills" search over name, description and root, the
  "N of M skills" count, the "N skills ship inside plugins" link to
  `#/plugins`, the app's order (Shared, Harness, Catalog, Project, Plugin,
  Built in, then inventory order) and its fold at 15 rows.
- **Harness links** in the header: "8 of 10 linked" with an amber dot when a
  folder is blocked. It opens the chips per harness skills folder (Linked,
  Not linked, Blocked), "Link all (N)" when any folder is free to link, and
  per chip the folder path, the app's `SkillRoot.explanation` sentence and
  its action: **Unlink** for a Linked folder, **Link to shared skills** for
  a Not linked one, nothing for a Blocked one. The header and popover are on
  the detail page too, as in the app. See "Writes" below.
- **Detail** (`#/skills?skill=<folder>`): the lock note, the header card
  (name, source badge, path, description, the app's per-source note and the
  link chips: every folder for a shared skill, the loading harnesses
  otherwise), the SKILL.md frontmatter fields (`key: value` lines, block
  scalars and wrapped plain values, as `MarkdownFrontmatter` reads them;
  lists and nested maps are left out; folded to 3), the SKILL.md text, and
  the Contents card (`PrimitiveTree`'s sections, folded to 3). A file opens
  read only in a sheet.

## Writes

swe links whole skills folders, never single skills, so every write
names a harness skills folder from the payload's `harnesses`. Each action
asks first, inline in the popover: a panel says exactly what will happen
(which folder, linked to what, whether it is backed up first) with Cancel
and the action's button. One write runs at a time: while the hub works
the chip spins and every write button is disabled; the page repaints only from the hub's answer. A
refusal shows in the popover and in an error card on the page, and the
list is read again, since a failed change may still have moved something
(the app reloads either way too).

`POST /v1/app/skills/<action>`, behind the device bearer and the Origin
check, serialised by a lock (the app's `SerialGate`, since every link
rewrites `skill_links.json`):

| Action | Body | Does |
|---|---|---|
| `link` | `{"harness": "<folder path>", "expect": "create", "expect_detail": "", "skill"?: "<skill folder>"}` | `link_skill_root(path, force=…)`, as `swe skills link <path> [--force] --json`. Only a Not linked folder (`create`, `relink`, `absorb`); `--force` only for `absorb`, which is backed up to `~/.swe/backups` first when it has anything in it (copies of shared skills, or empty subfolders, which swe's link otherwise refuses). |
| `unlink` | `{"harness": "<folder path>", "expect": "linked", "expect_detail": "", "skill"?: …}` | `unlink_skill_root(path)`, as `swe skills unlink <path> --json`. Only a Linked folder; the folder is left absent, not recreated. |
| `link_all` | `{"harnesses"?: ["<folder path>", …], "skill"?: …}` | `SkillsModel.linkAll`: each folder `linkAllTargets` clears (Not linked, not archived, and not a folder of shared copies), one at a time; a failure is reported and the rest go on. `harnesses` limits it to the folders the confirm named; one named that is no longer free, or no longer listed at all, is reported in `failed`, not linked. |

`harness` is a folder's path (a registry name works when only one folder
has it); it must be one the inventory lists (404). `expect` and
`expect_detail` are the state and detail the page showed (an empty folder
that fills with shared copies stays `absorb` but would now be backed up);
when either has changed since, nothing is written and the 409
`state_changed` says what the folder is now. `skill` must be a listed skill
folder (404). Other refusals: 409 with the folder's state as the code for
`keep` (holds skills that exist nowhere else: never overwritten),
`conflict` (a file in the way), `ignored` and `skipped`; 409
`already_linked`, `not_linked`, `link_failed`/`unlink_failed` (swe's own
error, such as an unreadable `skill_links.json`, which stops the write
before the folder moves); 422 for a malformed body; 404 for an unknown
action.

An archived harness's folder can be linked or unlinked with its own
button, as in the app (its explanation says swe init leaves it alone);
Link all never touches it.

The reply is the list payload read after the write (below), plus
`action`, `harness` (the folder's new row, or null for `link_all`),
`skill` (the asked-for skill's new row, so the detail's chips repaint, or
null) and `result`: the `LinkReply`/`UnlinkReply` `--json` shape
(`{"action": "link", "harness", "source", "target"}`,
`{"action": "unlink", "harness", "path", "mkdir": false}`), or for
`link_all` `{"linked": [{name, path}], "failed": [{name, path, message}],
"skipped": [<root rows>]}`, from which the page writes the app's summary
caption ("Linked 2 harnesses. 1 needs a look: claude (…)").

### New skill

"New skill" in the header (and in the empty list's card) opens the app's
`NewSkillSheet` as a dialog: Name (monospaced, with the app's hint until it
is a valid Agent Skills name: lowercase letters, digits and single hyphens,
at most 64), Description, a line saying which file it writes on which
machine, Cancel and Create. Create stays off until the name is valid. On
success the list repaints behind the sheet, which turns into "Created
~/.swe/skills/<name>/SKILL.md" with Open it (the skill's detail) and
Done.

`POST /v1/app/skills/new` with `{"name", "description"?}` runs
`skills.create.create`, the code `swe skills new <name>
[--description=…]` runs: `~/.swe/skills/<name>/SKILL.md` from its
template, written atomically, under the same lock as the links. A blank
description takes the template's default. Whatever `create` refuses (a bad
name, a name already taken, a multi-line or over-long description, no
`~/.swe` yet) is a 422 `bad_skill` with its reason. The reply is the list
payload plus `action: "new"`, `created` (the SKILL.md's path) and `skill`
(its row, or null if the inventory doesn't list it).

## Payload

`GET /v1/app/skills`:

```json
{"machine": "home-server", "home": "/home/dev",
 "shared_dir": "/home/dev/.swe/skills",
 "harnesses": [{"name": "codex", "label": "codex", "path": "…/.codex/skills",
                "state": "keep", "detail": "6 of 6 skills exist nowhere else", "archived": false}],
 "skills": [{"name": "tdd", "description": "…", "path": "…", "root": "shared",
             "source": "shared", "harnesses": ["claude", "codex"], "plugin": null}],
 "plugin_skill_count": 84, "catalog_error": null}
```

`skills` holds every skill; `plugin` is the ref of the plugin a skill
moves to, else null, and `plugin_skill_count` counts those. The page
hides them while `PAGES` has a `plugins` page.

`GET /v1/app/skills?skill=<folder>` adds `skill`, `editable` (the app's
`PrimitiveOwnership.isEditable`, which only picks the lock note's words),
`skill_file`, `skill_md` (text, or null when it can't preview),
`frontmatter` (`[{key, value}]` or null) and `tree`
(`[{kind, title, entries: [{name, path, folder, preview?, children?}]}]`,
paths relative to the folder), plus `machine`, `home`, `shared_dir` and
`harnesses` so the header renders on a deep link.

`GET /v1/app/skills?skill=<folder>&file=<relative path>` returns
`{name, path, outside, text}`.

Only a folder the inventory lists is accepted (404 otherwise). A file must
stay inside that folder once symlinks resolve (`outside: true` and no text
otherwise; an absolute name is a 400). Text is read only under 1 MB with no
NUL byte in the first 8 KB, as `PrimitiveTree.isTextFile` decides.

## Differences from the Mac app

These writes are left out; they stay in `swe skills` and the Mac app:

- Editing the frontmatter form and the folder's files (Raw/Rich editor,
  Save, Revert, Move to Trash). The Mac app lists the files in a rail
  beside an inline editor; the web app keeps its Contents card and sheet.
- Show in Finder, and the sheet's "Open in <editor>" after New skill;
  Open it shows the skill's detail instead.

Also:

- Link, Unlink and Link all ask first, in place, naming what will happen;
  the app's buttons act on the first click. The hub refuses a folder whose
  state changed since the page read it, where the app acts on its last
  list.
- An `absorb` folder the app calls empty but that holds empty subfolders
  is linked with `--force` (backed up first); the app's link fails on it.
- A failed write also shows its error card on the detail page; the app
  shows it over the list and in the popover only.
- The skills come from the hub machine, so the subtitle names it rather
  than "this Mac".
- Project skills (`<cwd>/.cursor/skills`) are read with `/` as the working
  folder, as the app's `swe` runs, so none is listed.
- A back link replaces the app's ⌘[ and sidebar reselect; the detail has
  its own URL.
- SKILL.md is shown in its own card on the detail page; in the app it opens
  from Contents.
- Paths are not middle-truncated by measured width; a long one is cut to
  90 characters with both ends kept.
