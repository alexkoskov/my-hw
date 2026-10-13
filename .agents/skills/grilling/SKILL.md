---
name: grilling
description: Stress-test a plan, feature, or decision through a focused interview. Use when the user asks to grill their thinking ("погрилль", "разбери план вопросами"), or an active planning workflow needs unresolved user decisions. Clear tasks and researchable facts alone do not trigger an interview.
---

# Grilling

Clarify the decisions needed for the user's current scope. Use the user's language. When
called by another planning skill, supply its interview method; that skill owns the document
format, saved state, validation, and approval steps.

## Interview

1. Use the conversation and relevant existing artifacts to identify the goal, settled
   decisions, and consequential gaps. Resume from what is already known.
2. Organize the gaps by dependency: settle a decision before asking questions whose answers
   depend on it. Research factual questions yourself with the tools available in the current
   session. Use subagents or another chat only when the user explicitly authorizes them.
3. Ask a small round of independent, currently answerable questions, usually two or three.
   For each, state the decision, your recommended answer, and its main tradeoff. Follow the
   calling workflow's question format when it specifies one. Wait for the user's answers
   before the next round.
4. Challenge a weak answer with a concrete scenario, counterexample, or relevant source
   evidence. Once the user has considered the tradeoff and made a choice, use that choice
   to update the remaining questions.
5. If the user is unsure, explain the practical options. Resolve essential gaps; record
   optional uncertainty or an explicitly accepted assumption rather than inventing an answer.

Depth should fit the task. Focus on decisions that change the requested behavior,
feasibility, scope, or acceptance criteria. New ideas outside that scope are optional
follow-up work.

## Finish

Finish when the agreed scope and success criteria are clear and consequential blockers are
resolved or explicitly accepted. Summarize the agreed decisions and remaining assumptions
for the user to correct.

In an existing planning workflow, continue at its next step and reuse its records and
approval gate. In standalone use, return the clarified plan. If the user already authorized
a subsequent action, continue within that authorization; otherwise finish with the plan.
The interview itself does not add an approval gate or authorize implementation or publishing.

Adapted from [Matt Pocock's grilling](https://github.com/mattpocock/skills/blob/d81f3a183412e71a5b1e84ca21bc1a35eea03a60/skills/productivity/grilling/SKILL.md)
for the current-session, minimum-sufficient workflow. The upstream MIT license is preserved
in `LICENSE`.
