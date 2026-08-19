---
description: Behavioral guidelines to reduce common LLM coding mistakes. Use when writing, reviewing, or refactoring code to avoid overcomplication, make surgical changes, surface assumptions, and define verifiable success criteria.
---

# Karpathy behavioral guidelines

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly - in a line, not a preamble.
- Make routine judgment calls yourself. Surface a choice only when different readings of the request would lead to materially different work; otherwise pick the sensible default and name it.
- If a simpler approach exists, say so. Push back when warranted.
- If something is genuinely unclear and no assumption is safe, stop. Name what's confusing. Ask.

The threshold matters in both directions: asking about every fork stalls work that a careful colleague would just do, and picking silently on a real fork delivers the wrong thing.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- A linter/scanner finding is advisory input, not an automatic mandate. A change
  whose only purpose is to silence a tool (an exemption comment, over-tightening
  best-effort code) is itself overcomplication - fix the real issue or skip it
  deliberately, stating why.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" -> "Write tests for invalid inputs, then make them pass"
- "Fix the bug" -> "Write a test that reproduces it, then make it pass"
- "Refactor X" -> "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
1. [Step] -> verify: [check]
2. [Step] -> verify: [check]
3. [Step] -> verify: [check]

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

**Verification means an artifact, not a re-read.** A test that runs, a command whose exit code you check, a diff compared against the spec - that is verification. Re-reading your own output to double-check it, or handing work you just finished to a subagent for review, is not: the model already verifies itself, and a second pass of the same kind burns tokens without changing the outcome. If you cannot name the artifact that proves a step is done, the criterion is still weak - fix the criterion instead of adding a review step.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
