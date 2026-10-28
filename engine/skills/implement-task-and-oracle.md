---
name: implement-task-and-oracle
description: Implement one approved Cyber Bench task and drive its oracle to success in one agent run, delegating any instruction or writeup edits to first-party Muse.
---

**Source of truth: the `team-aai:cyber-bench-guide` skill**

The action runner supplies that source of truth as an embedded `<cyber-bench-guide>` block because the installed skill intentionally disables direct model invocation. When the block is present, read and follow it directly; do not attempt to invoke the installed skill. If the block is absent, load the guide through an available first-party source before editing.

# Implement One Task and Pass Its Oracle

Own exactly one task per invocation: the current task directory named by `SYNTHETIC_TASK`. Never implement another selected task or edit a neighboring task. Batch selection is handled by the caller, which must invoke this skill independently once for every eligible task.

## Implement

Follow the supplied `team-aai:cyber-bench-guide` content before editing the task. It is authoritative for the host format, Cyber Bench naming and metadata, model-role restrictions, separate-verifier defaults, scaffolding command, file layout, and validation. Do not substitute a generic T-Bench or Codimango scaffold for its Cyber Bench scaffold.

Read this task's `design.md`, the approved `template.md` in its parent task-set directory, and the implementation request included with this invocation.

### Task-set implementation guidance

Before scaffolding or editing, check for `impl-guidance.md` in the current task's parent task-set directory, beside `template.md` (`$SYNTHETIC_TASK_PATH/../impl-guidance.md`). If present, read the entire file and follow all its rules throughout this implementation-and-oracle invocation, including required implementation delegations, validation, and oracle repairs. The guidance applies to every task implemented in this task set.

If the file is absent, proceed without task-set-specific implementation rules. Do not create it or search other task sets or reference tasks for fallback guidance.

Treat the file as read-only input consumed only by this skill during the implementation step. Do not load or apply it in other workflow steps: template authoring, design generation, redesign, taxonomy generation, benchmarking, analysis, or review. Apply its rules together with the approved design, template, and Cyber Bench guide; local guidance does not relax the guide's model-role or protected-authoring boundaries.

### Reference task or fresh scaffold

If the implementation request explicitly supplies a reference task, use that reference as the starting scaffold instead of fully scaffolding a new task. Never infer a neighboring task as the reference. Do not edit the reference in place: copy its applicable Cyber Bench structure into the current task, preserve this task's `design.md` and `design-status.json`, and adapt all task-specific names, metadata, taxonomy, tags, challenge content, verifier, solution, and artifacts. Remove inherited results, `.review` output, secrets, stale identifiers, and other reference-only state. Validate the result against `team-aai:cyber-bench-guide`; a reference task is a starting point, not permission to retain incompatible or stale fields.

If no reference task is supplied and `task.toml` is absent, scaffold only through the `scaffold_cyber_task` flow defined by `team-aai:cyber-bench-guide`, using the Cyber Bench SSOT templates and the appropriate host/team/profile axes. Merge the generated Cyber Bench scaffold into the current task without replacing `design.md` or `design-status.json`. Do not use `codimango task init` and do not hand-roll a generic scaffold.

If a prior attempt already created task files, preserve the useful work and bring the existing layout into compliance with `team-aai:cyber-bench-guide` rather than scaffolding over it.

Implement the complete task described by the approved design, including its challenge, environment, verifier, metadata, solution, tests, and required supporting artifacts. Validate the implementation before running the oracle.

## Model-role routing

Apply the model-role rules in `team-aai:cyber-bench-guide` to every file. Use `SYNTHETIC_AGENT` to identify the selected implementation agent. When that model is not allowed to author a required file, delegate that file to a fresh eligible first-party Muse session rather than writing it yourself. In particular, Claude must not author `tests/verify.py`, `tests/test.sh`, or a grading rubric, and no third-party model may author `instruction.md`, `writeup.md`, evaluated-agent-visible content, or training-corpus content.

Do not token-launder: give a 1P delegate the original human-approved design, template, task-set `impl-guidance.md` when present, reference task when supplied, and the Cyber Bench guide—not third-party-generated draft text, findings, diffs, or editorial instructions. Keep each delegation's allowed paths explicit and disjoint. Anything drafted by 1P remains subject to human review as required by the guide.

## Protected Muse delegation

Never draft or edit `instruction.md` or `writeup.md` yourself. Delegate review of those files and any needed drafting or corrections to a fresh first-party Muse session on every invocation. The Muse delegate may leave already-valid files unchanged. This applies even when the implementation agent is Muse: the protected documents require a separate Muse invocation.

Launch the delegate as a separate process scoped to the current task workspace:

```bash
muse exec --provider meta --workspace "$SYNTHETIC_TASK_PATH" --approval-mode never --no-session-log --disable-request-user-input --trust-workspace "<protected authoring prompt>"
```

Tell it to read the approved parent `template.md`, this task's `design.md`, the original parent `impl-guidance.md` when present, and only the implementation context needed for accurate paths and artifacts. Require it to follow the guidance's applicable rules as part of this implementation step. The delegate may edit exactly `instruction.md` and `writeup.md`; it must not edit any other file.

Require the delegate to:

- copy the shared participant-facing body between `<!-- BEGIN SHARED INSTRUCTION -->` and `<!-- END SHARED INSTRUCTION -->` from the template verbatim;
- add only permitted participant-facing task specifics, without revealing the vulnerability, intended solution, verifier internals, grading, or other protected answers;
- draft a task-specific researcher writeup with a human-review notice and claims grounded in the implemented task; and
- verify both files are non-empty and no file outside those two paths changed during its work.

Check the delegate's exit status and perform only deterministic boundary validation before continuing. If the delegate fails, do not draft the protected files yourself or feed it third-party editorial feedback; retry from the original approved sources or report the blocker.

## Oracle repair loop

Run the Codimango oracle for this task from the task-set directory, retaining its output under this task's `.review/jobs/oracle` directory:

```bash
codimango bench run -p "$SYNTHETIC_TASK" -a oracle -k 1 -o "$SYNTHETIC_TASK_PATH/.review/jobs/oracle"
```

Read the actual command result and oracle output.

Do not finish after a failed oracle run. Diagnose the failure, repair this task, rerun relevant focused checks, and run the oracle again. If a repair requires any change to `instruction.md` or `writeup.md`, use a new first-party Muse delegation under the same boundary above. Continue until the oracle succeeds, unless an external blocker makes further repair impossible; in that case, clearly report the blocker and fail the run instead of claiming success.

## Completion

Finish successfully only when this one task is fully implemented and its latest oracle run passes. Keep implementation work, Muse delegation, oracle output, and repair attempts in this invocation's single transcript.

After verifying the passing oracle result, end the response with this exact standalone line:

```text
SYNTHETIC_RESULT={"oracle_passed":true}
```

Never emit that marker when the oracle failed, was skipped, or could not be verified. The caller treats a missing marker as a failed step.
