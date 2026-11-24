# Drive plan file - format spec (v1)

Instinct writes this file from `<run>/inventory.json`. The swarm only validates it and executes it
exactly. One JSON object. Paths are folder paths relative to the inventory scope (the Drive root, or
the test folder), with `/` between folders. `""` means the scope root itself.

```json
{
  "schema": "pcmcp.drive_plan",
  "schema_version": 1,
  "author": "Instinct",
  "written_at": "2026-11-10T20:00:00Z",
  "inventory_hash": "<copied from inventory.json>",
  "scope_root_id": "<copied from inventory.json>",

  "mode": "full",
  "new_folders_declared": ["3D assets/Characters", "Projects/Some New Project", "Review - Not Sure/Archive 2019"],
  "create_folders": ["Pics & Videos", "Projects", "Projects/Synomize", "SynHumanity Labs", "3D assets",
                     "3D assets/Characters", "Projects/Some New Project", "Review - Not Sure", "Review - Not Sure/Archive 2019"],

  "placements": { "Projects/Synomize": ["<fileId>", "<fileId>"], "Review - Not Sure": ["<fileId>"] },
  "archives":   { "Review - Not Sure/Archive 2019": ["<fileId>"] },
  "copies":     { "SynHumanity Labs": ["<fileId>"] },
  "stay":       ["<fileId>"],

  "tags":  { "<fileId>": ["synomize"] },
  "notes": { "<fileId>": "why it goes there (for Angie; the swarm ignores notes)" }
}
```

| Key | Meaning |
|---|---|
| `mode` | `"full"` (default): every in-scope file needs a destination. `"folders_only"`: creates folders and moves nothing (placements, archives, copies, stay must be empty). `drive plan folders --name X` writes one. |
| `new_folders_declared` | **Every folder this plan creates that is not in `folder-map.json`**, listed at the top so Angie approves them all (new Projects subfolders, 3D Assets subfolders...). |
| `create_folders` | Every folder that must exist. Ones that already exist at that exact path are reused, not duplicated. Parents must be listed or already exist. |
| `placements` | Destination → file ids to **move** there (metadata only: add new parent, remove old). Folder ids may be listed too (the folder moves with its contents). |
| `archives` | Same as placements, executed by the Archive lead (E). Archive = a metadata-only move into an archive folder. Nothing is zipped. |
| `copies` | Destination → file ids to **copy** there (server-side copy; the original stays where `placements` says). Copies use Drive storage quota. |
| `stay` | Files that stay exactly where they are. |
| `tags` | Optional labels the never-move rules read: `synomize`, `synatoms`, `personal-ai`, `synomize-3d`. |

## The validator (`drive plan validate`) refuses a plan if

- `MISSING_DESTINATION`: any in-scope file is not in exactly one of `placements` / `archives` / `stay` (`Review` counts).
- `DUPLICATE_FILE` / `UNKNOWN_FILE`: a file is listed twice, or is not in the inventory scope.
- `DELETE_FORBIDDEN`: any key or `action`/`op` names delete, trash, purge, erase... Nothing is ever deleted.
- **Skipped, not refused:** a direct move or copy that would take a 3D asset attached to Synomize out of Synomize is
  dropped. The file stays where it is, the plan stays valid, and the list goes to `<run>/skipped-kept-in-synomize-<hash>.txt`
  (also in `dryrun.json` and `summary.json`). If such a file would ride along inside a folder the plan moves, the plan
  is refused instead, because a skip can't keep it in place.
- `NEVER_MOVE:legal-proof-locked`: anything inside `LEGAL PROOF - DO NOT TOUCH` would move out of it.
- `NEVER_MOVE:<rule>`: a file's **final** folder (including files that ride along inside a moved folder) breaks a
  rule in `folder-map.json`: Synomize/SynAtoms items into `Projects/VectorVerse`, personal-AI items into `PAC`.
  Rules match the plan's tags OR the file's name/current path. They can only refuse; they never choose a folder.
  The sub-bot checks again right before every move.
- `UNDECLARED_FOLDER`, `DEST_NOT_IN_MAP`, `FOLDER_NOT_LISTED`: a destination or created folder is outside the map and
  not declared, or a destination neither exists nor is created.
- `FOLDER_BEING_MOVED`: a destination or new folder sits at or inside a folder this plan moves (its real final location would differ from its path, so it is refused).
- `AMBIGUOUS_FOLDER`, `CYCLE`, `BAD_PATH`, `SCHEMA`, `INVENTORY_MISMATCH` (plan written from a different inventory).

The plan's identity is `sha256` of its canonical JSON. Angie's approval names that hash, so any edit needs a new approval.
