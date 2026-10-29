---
name: generic-writeup
description: Draft a short, plain-English task writeup in five sections.
---

# Task Writeup

Write or rewrite writeup.md from verified task facts. Target about 60 lines total.

## Before writing

Read task.toml, README.md, instruction.md, the implementation sources, solution/,
and tests/. Check paths, expected outputs, and constants against those sources.
Report factual inconsistencies instead of silently inventing an explanation.

## Fixed structure

```markdown
# Writeup: <Task Name>

# Setup & intent

# Reconnaissance

# Discovery reasoning

# Minimal proof (PoC)

# Verification
```

The title carries only the task name.

- **Setup & intent**: 2–3 short sentences describing the solver's output and how
  the application judges it. Omit taxonomy and history.
- **Reconnaissance**: one flat bullet list, from external behavior to internal
  findings. Avoid extra subheadings.
- **Discovery reasoning**: use a slim table when it clarifies the task's input
  or output format, then 2–4 bullets explaining derivation and checking.
- **Minimal proof (PoC)**: at most three bullets describing the working proof,
  how to reproduce it, and the actual acceptance result. Include exact artifacts
  or commands needed to reproduce it. If there is no bug to exploit, say so once.
- **Verification**: at most two bullets describing successful and invalid cases
  and what the verifier checks. Omit build-gate and calibration internals.

## Style rules

- Use short sentences, simple English, and one idea per bullet.
- Prefer plain terms such as reference solution, real/fake code, running number,
  and totals when they accurately describe the facts.
- Keep exact relevant paths, sizes, values, and checksums. Cut unnecessary seeds,
  indices, and other low-level mechanics. Describe behavior clearly.
- Verify every path and number. Use clean grammar.

## Never include

- Revision/design history, internal coordinates, or task numbering.
- Calibration results, pass rates, trial counts, or model names.
- Rewrites of the author's own intent prose beyond requested grammar fixes.

## After writing

Include a notice that human review is needed. Report the line count and section
list. The engine commits the generated file as requested by this workflow; the
author still needs to review it. Do not stage or commit from the agent invocation.
