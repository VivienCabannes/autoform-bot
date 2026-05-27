# Statement Extraction

The statement extraction pipeline identifies and extracts explicitly labeled mathematical statements (theorems, lemmas, definitions, etc.) from LaTeX or Markdown textbooks and produces a structured YAML target list. This target list feeds into downstream pipelines: autoformalization (which translates statements into Lean 4 code) and evaluation (which grades the results).

The pipeline uses multi-agent consensus with reviewer arbitration to maximize both recall and precision, and handles overlapping text chunks to avoid losing statements at chunk boundaries.

## Pipeline Overview

```
Book files (.md/.tex)
    |
    v
+------------+
|  Chunking  |   Split concatenated book text into overlapping pieces
+-----+------+
      |  N chunks
      v
+------------+
| Extraction |   k independent agents per chunk, all in parallel
+-----+------+
      |  N x k extraction results
      v
+-----------------+
| Reconciliation  |   Per-chunk: unanimous consensus + reviewer for disputes
+-----+-----------+
      |  N curated statement lists
      v
+----------+
|  Merging |   Adjacent-chunk deduplication across overlap regions
+-----+----+
      |  Final ordered list
      v
  targets.yaml + review.yaml + cost.json
```

## Pipeline Steps

### Step 1: Chunking

**Source:** `chunking.py`

The pipeline discovers all `.md` and `.tex` files in the book directory (sorted alphabetically), concatenates them into a single list of lines, and splits that into fixed-size overlapping chunks.

**Parameters:**
- `chunk_size` (default 500): number of lines per chunk
- `overlap` (default 50): number of lines shared between consecutive chunks

**How it works:**

Each chunk starts at an offset that advances by `chunk_size - overlap` lines from the previous chunk. For example, with defaults, chunk 0 covers lines 1-500, chunk 1 covers lines 451-950, chunk 2 covers lines 901-1400, and so on. The overlap ensures that any statement spanning a chunk boundary appears fully in at least one chunk.

Each chunk is represented as a frozen dataclass (`Chunk`) with fields: `index`, `text` (the joined lines), `start_line` (1-indexed), and `end_line`.

### Step 2: Extraction

**Source:** `extraction.py`

For each chunk, `k` independent LLM agents extract statements in parallel. Each agent receives the chunk text inline in its prompt (no filesystem tools needed) and returns a YAML list of statements.

**Key behaviors:**
- All `N x k` agents run concurrently, bounded by a shared `asyncio.Semaphore` (the `--concurrency` parameter).
- Each agent is a single-turn, tool-free call: one prompt in, one YAML response out.
- Each agent gets its own trace file saved under the run's trace directory for cost tracking and debugging.
- The extractor agent prompt (`agents/extractor/prompt.md`) instructs agents to extract only explicitly labeled statements (Theorem, Lemma, Proposition, Definition, Corollary, Axiom, Conjecture, Construction, Claim) and to exclude proofs, remarks, examples, and exercises.

**Per-statement output fields:**
- `name`: the full label as it appears, e.g. "Theorem 3.2 (Heine-Borel)"
- `description`: the complete statement text (hypotheses, conditions, conclusions; no proof)
- `location`: inferred from surrounding headings, e.g. "Chapter 3, Section 2"
- `kind`: one of theorem, lemma, proposition, definition, corollary, axiom, conjecture, construction, claim

### Step 3: Reconciliation

**Source:** `reconciliation.py`

Reconciliation resolves disagreements among the `k` independent extractions for each chunk. It runs in two phases:

**Phase 1 -- Consensus check.** Statements are grouped by normalized name (see Name Normalization below). A statement is accepted into the consensus set if **all k agents** found it. Any statement found by fewer than k agents is marked as disputed.

**Phase 2 -- Reviewer arbitration.** For chunks with disputed statements, a reviewer agent (`agents/reviewer/prompt.md`) receives:
- The original chunk text
- Each disputed statement with the list of agents that found it and their extracted versions

The reviewer reads the source text and renders an include/exclude verdict for each disputed statement. Included statements are added to the chunk's curated list alongside the consensus statements.

**Output:** For each chunk, a single curated list (consensus + reviewer-included). Chunks with disputes also produce a `ChunkConflict` record for the review report.

All chunk reconciliations run in parallel.

### Step 4: Merging

**Source:** `merging.py`

Because chunks overlap, the same statement can appear in two adjacent chunks' curated lists. The merging step deduplicates across chunk boundaries.

**How it works:**

1. Chunk 0's statements go directly into the final list (no deduplication needed).
2. For each subsequent pair (chunk k, chunk k+1):
   - If the chunks do not overlap (chunk k+1 starts after chunk k ends), or if either chunk has no statements, chunk k+1's statements are appended as-is.
   - If the chunks overlap and both have statements, a merger agent (`agents/merger/prompt.md`) compares the two statement lists. The agent identifies which statements from chunk k+1 are duplicates of statements already in chunk k, based on mathematical content (not just name matching).
3. Duplicates identified by the merger are removed from chunk k+1's list. The remaining (genuinely new) statements are appended to the final list.

All merge pairs are independent (each reads the original per-chunk statements, not accumulated results) and run in parallel.

**Duplicate filtering details:** The merger agent returns a YAML list of duplicates with verdicts. Statements with verdict "new", "keep", "not duplicate", or "not a duplicate" are retained. All others are removed. Name matching during filtering uses the normalized form, and the code also handles cases where the model concatenates name and description with a colon separator.

### Step 5: Output

The pipeline writes several files:

- **`targets.yaml`** -- the final ordered list of extracted statements
- **`review.yaml`** -- details of chunks where agents disagreed (disputed statements, which agents found them, the different versions extracted), written only if there were conflicts
- **`cost.json`** -- aggregate token usage and USD cost across all agent calls
- **`traces/<timestamp>/`** -- individual per-agent trace files (JSON) for debugging and auditing

## Chunking Strategy

The overlap between chunks serves a specific purpose: if a mathematical statement straddles a chunk boundary (e.g., a theorem starting at line 498 and ending at line 510), it will appear completely in one chunk even though it gets split across the boundary in the other. With 50 lines of overlap and 500-line chunks, any statement of up to 50 lines is guaranteed to appear fully in at least one chunk.

The tradeoff is that statements in the overlap region will be extracted by agents for both chunks, requiring the merging step to remove the duplicates. The overlap size should be tuned based on typical statement length in the source material.

## Extraction: Multi-Agent Voting

Running `k` independent agents per chunk (default k=4) serves as a reliability mechanism:

- **High-confidence statements** (labeled "Theorem 3.2", etc.) are found by all k agents and pass through consensus automatically.
- **Edge cases** (unusual labels, ambiguous formatting) may be caught by some agents but not others. The reviewer arbitrates these cases with access to the source text.
- **Hallucinated statements** (an agent inventing a nonexistent statement) are unlikely to be independently invented by multiple agents. If only one agent reports a statement, the reviewer checks it against the source text.

The parameter k controls the tradeoff between cost and thoroughness. Higher k increases both extraction cost and confidence in the consensus.

## Merging: Cross-Chunk Deduplication

The merger agent prompt instructs it to focus on mathematical content, not names. Two statements with different names ("Theorem 2 (Chow's Lemma)" vs "Theorem (Chow's Lemma)") are still duplicates if they state the same mathematical fact. Conversely, two statements in related areas are not duplicates unless they describe the exact same result.

The merger operates conservatively: when in doubt, it keeps the statement (erring on the side of including a potential duplicate rather than losing a genuine statement).

## Reconciliation: Resolving Disagreements

The reviewer agent acts as a tiebreaker. It receives the full chunk text alongside the disputed statements, giving it ground truth to make informed include/exclude decisions. The reviewer's prompt mirrors the extractor's criteria: only explicitly labeled mathematical statements count.

The reviewer returns structured YAML with a verdict per statement. Excluded statements are logged with reasons. The `review.yaml` output file preserves dispute details for human auditing.

## Output Format

The `targets.yaml` file contains a YAML list of statements. Each entry includes only non-empty fields:

```yaml
- name: "Theorem 3.2 (Heine-Borel)"
  description: "A subset of R^n is compact if and only if it is closed and bounded."
  location: "Chapter 3, Section 2"
  kind: "theorem"
- name: "Definition 3.1"
  description: "A topological space X is called compact if every open cover of X has a finite subcover."
  location: "Chapter 3, Section 1"
  kind: "definition"
```

The underlying data type is `FormalizationTarget` (defined in `autoform/eval/types.py`) with fields: `name`, `description`, `kind`, `location`, `lean_declaration` (optional, populated downstream), and `lean_file` (optional, populated downstream). The `save_task_list` function serializes it, omitting `None` and empty-string fields.

## Name Normalization

**Source:** `normalization.py`

Statement names are normalized for deduplication comparisons. The normalization rules:

1. **Expand abbreviations:** Thm/thm -> theorem, Lem -> lemma, Prop -> proposition, Def/Defn -> definition, Cor -> corollary, Conj -> conjecture, Ax -> axiom, Const -> construction, Cl -> claim.
2. **Strip parenthetical nicknames when a number is present:** "Theorem 3.2 (Heine-Borel)" becomes "theorem 3.2", but "Lemma (Noether Normalization)" keeps the parenthetical since it has no number and the nickname is the only identifier.
3. **Lowercase and normalize whitespace.**
4. **Strip trailing punctuation** (periods, colons, semicolons, commas).

| Input | Normalized |
|---|---|
| `Theorem 3.2 (Heine-Borel)` | `theorem 3.2` |
| `Thm. 3.2` | `theorem 3.2` |
| `Def. (Presheaf)` | `definition (presheaf)` |
| `Lemma (Noether Normalization)` | `lemma (noether normalization)` |

## YAML Parsing

**Source:** `parsing.py`

LLM responses are not always clean YAML. The parser tries three strategies in order:

1. **Code fences:** extract content from `` ```yaml `` or `` ``` `` fences and parse it.
2. **Whole response:** parse the entire response as YAML.
3. **Fragment extraction:** find runs of consecutive lines matching YAML list item syntax (`- key: value`) in prose text, join them, and parse.

The parser also handles a common failure mode: LaTeX backslashes inside double-quoted YAML strings (e.g., `\mathbb`) which are invalid YAML escape sequences. It retries parsing after doubling all backslashes inside double-quoted regions.

## Usage

```bash
# Extract statements from a single book
python -m autoform.statement_extraction run \
    --book_dir=autoform/data/algebraic_topology_I

# With custom parameters
python -m autoform.statement_extraction run \
    --book_dir=autoform/data/algebraic_topology_I \
    --model="Opus 4.6" \
    --k=4 \
    --chunk_size=500 \
    --overlap=50 \
    --concurrency=5 \
    --output=custom_output.yaml
```

### CLI Parameters

| Parameter | Default | Description |
|---|---|---|
| `--book_dir` | (required) | Path to the book directory containing `.md` or `.tex` files |
| `--model` | `"Opus 4.6"` | LLM model name (resolved via `lookup_model`) |
| `--k` | `4` | Number of independent extraction agents per chunk |
| `--chunk_size` | `500` | Number of lines per chunk |
| `--overlap` | `50` | Number of overlapping lines between consecutive chunks |
| `--concurrency` | `5` | Maximum number of concurrent agent calls (shared across all pipeline stages) |
| `--output` | `<book_dir>/targets.yaml` | Path for the output targets file |

## File Structure

```
autoform/statement_extraction/
  __main__.py          CLI entry point and pipeline orchestration
  chunking.py          Book file discovery and overlapping chunk creation
  extraction.py        Per-chunk multi-agent extraction
  reconciliation.py    Within-chunk consensus and reviewer arbitration
  merging.py           Cross-chunk deduplication of overlap regions
  normalization.py     Statement name normalization for matching
  parsing.py           Robust YAML parsing from LLM output
  agents/
    extractor/         Extracts labeled statements from inline text
      prompt.md        Agent system prompt
      config.yaml      Model and turn configuration
    reviewer/          Resolves within-chunk extraction disputes
      prompt.md
      config.yaml
    merger/            Identifies duplicates across adjacent chunks
      prompt.md
      config.yaml
```

## Agents

All three agents are declarative (prompt.md + config.yaml, no Python subclass), single-turn (`max_turns: 1`), and tool-free (text is passed inline in the user message).

| Agent | Purpose | Input | Output |
|---|---|---|---|
| **extractor** | Extract all labeled statements from a chunk | Chunk text with line range | YAML list of statements |
| **reviewer** | Resolve disputes where agents disagree | Chunk text + disputed statements with agent versions | YAML list with include/exclude verdicts |
| **merger** | Identify duplicates across overlapping chunks | Two statement lists from adjacent chunks | YAML list of duplicates to remove |

## Notes

- **Avoid duplicate source files.** If a book directory has both a concatenated file (`book.tex`) and individual part files (`part1.tex`, `part2.tex`, ...), remove one set to avoid processing every statement twice.
- **Concurrency** controls the total number of concurrent agent calls across all pipeline stages. With k=4 and 20 chunks, there are 80 extraction calls; setting concurrency=5 means at most 5 run simultaneously.
- **Traces** are saved per agent call under `traces/<timestamp>/`. Each trace file is a JSON document containing the prompt, response, token counts, and cost. The `cost.json` file aggregates these across the entire run.
- **The consensus threshold is unanimous.** A statement must be found by all k agents to pass consensus without review. This is conservative by design; the reviewer handles partial-agreement cases.
