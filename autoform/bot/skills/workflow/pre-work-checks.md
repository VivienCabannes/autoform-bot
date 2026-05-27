# Pre-Work Checks

Always verify the current state before writing any code.

## Check if already done

- Grep for `theorem` vs `axiom` on the target. Check for sorrys. If already proved on main, make minimal changes and move on.
- Read `skills/tasks/<task_id>/guide.md` if it exists — it contains lessons from prior attempts.

## Check dependencies

- Grep for lemmas your proof needs and verify they're not themselves sorry'd. If critical dependencies are sorry'd, report the blocker instead of attempting the proof.
- Check git log for recent changes that may have already addressed the task.

## Verify statement correctness

- Try small counterexamples (n=0, zero function, degenerate cases) and compare with the textbook.
- If the statement appears false as formalized, leave it as sorry and document the issue in a comment.
