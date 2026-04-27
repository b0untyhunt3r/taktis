---
id: 12cd13ba3b1a504a8dc86621d61c7962
slug: ROADMAPPER_REVISION
name: Roadmapper Revision
description: Revises a roadmapper plan based on plan checker feedback
auto_variables: ["description", "expert_options"]
internal_variables: ["issues", "previous_plan_text"]
---
You previously generated a project plan that was reviewed by a plan checker.
The checker found issues that need to be addressed.

<project_description>
{description}
</project_description>

<interview_transcript>
{interview}
</interview_transcript>

{synthesizer}

<verification_issues>
{issues}
</verification_issues>

<previous_plan>
{previous_plan_text}
</previous_plan>

{expert_options}

Revise the plan to address ONLY the flagged issues. This is a surgical fix, not a rewrite.

## REVISION RULES — READ BEFORE CHANGING ANYTHING

1. **ONLY fix what the checker flagged.** Do not reorganize, rebalance, or
   restructure anything that wasn't called out as an issue.

2. **NEVER change wave assignments unless the checker explicitly flagged a
   dependency or collision issue for that specific task.** If the checker
   said "task X has a vague prompt" — fix the prompt, keep the wave number.

3. **NEVER split parallel tasks across waves or phases.** If 9 tasks were
   all wave 1 in the previous plan and the checker didn't flag a dependency
   problem between them, they MUST stay wave 1 in your revision.

4. **NEVER add new waves, new phases, or new tasks** unless the checker
   explicitly requested them.

5. **Copy unchanged tasks verbatim** from the previous plan — same prompt,
   same wave, same expert. Only modify the specific tasks the checker flagged.

Think of this as a code review fix: change the minimum needed to address
the feedback. A 3-line fix, not a full rewrite.
