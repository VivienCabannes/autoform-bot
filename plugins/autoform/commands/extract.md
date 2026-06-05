---
description: Extract a textbook's theorems & definitions into a targets.yaml.
argument-hint: "<book-dir> [--output FILE] [--backend native]"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task
---

# /autoform:extract — statement extraction

Turn a textbook (`book.md` / LaTeX) into a `targets.yaml` list of formalization targets via
autoform-bot's consensus extraction pipeline. Arguments: `$ARGUMENTS`.

Resolve inputs first and **echo them back**: book directory, output path (default
`<book-dir>/targets.yaml`), and backend (**default `python`**).

## Default — python backend (the autoform-bot engine)

By default this runs autoform-bot's extraction pipeline. Preflight, then run it:

1. Confirm you're in (or point `--repo-dir`/cwd at) an autoform-bot checkout with deps installed
   (`uv sync`) and the inference API key(s) the run needs.
2. Print the command, then run it (or print only with `--dry-run`):

```bash
python -m autoform.statement_extraction run \
    --book-dir=<book-dir> \
    --output=<output>
```

3. On completion, report where `targets.yaml` landed and the statement count.

## Opt-in — native backend (`--backend native`)

Runs the extraction loop in this Claude session instead of the Python engine (no autoform-bot
checkout needed). Phased:

**Phase 1 — Chunk.** Split the book into overlapping chunks (keep an overlap so boundary
statements survive). Produce a chunk-list artifact (index → byte/line range).

**Phase 2 — Extract (fan-out).** Per chunk, dispatch `k` (default 3) **extractor** subagents in
parallel; each returns a YAML list of labeled statements (name, description, location, kind).

**Phase 3 — Reconcile.** Accept statements all `k` agreed on; route **disputed** ones to one
**extraction-reviewer** subagent that rules include/exclude against the source.

**Phase 4 — Merge.** Per adjacent chunk pair, dispatch a **merger** subagent to drop duplicates
from the later chunk (judged by mathematical content, not label).

**Phase 5 — Emit.** Write `targets.yaml` and print a **summary table**: chunks, raw extractions,
disputed/resolved, duplicates removed, final count.

## Next

`targets.yaml` feeds `/autoform:orchestrate` (whole-book run) or `/autoform:formalize`.
