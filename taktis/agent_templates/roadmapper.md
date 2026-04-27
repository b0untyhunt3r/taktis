---
id: 77b24113f36e52de9e7314c6a510ba98
slug: ROADMAPPER
name: Roadmapper
description: Generates requirements, roadmap, and executable plan from research synthesis
auto_variables: ["description", "expert_options"]
internal_variables: []
---
<project_description>
{description}
</project_description>

<interview_transcript>
{interview}
</interview_transcript>

{synthesizer}

If research is provided above, you MUST use it:
- Adopt the recommended tech stack unless the interview explicitly contradicts it
- Respect the suggested phase ordering from the synthesis
- Incorporate risk mitigations into early phases, not as an afterthought
- Add investigation tasks for areas flagged as uncertain
- Where research and interview conflict, the interview (user's intent) wins

Adapt plan complexity to match the interview depth:
- If the interview was Quick (3-5 questions, brief answers), prefer fewer phases
  (1-3), leaner task prompts, and skip optional phases like dedicated testing or
  documentation unless explicitly requested. Focus on the core deliverable.
- If the interview was Deep (10-15 questions, detailed answers), use the full
  range of phases (up to 8) and thorough task prompts as the detail warrants.
Gauge depth from the interview transcript length and question count above.

{expert_options}

Before writing requirements, perform a brief state-gap analysis:
1. List what currently exists (files, capabilities, infrastructure — whatever
   dimensions are relevant to this project). If this is a greenfield project,
   state that explicitly.
2. List what must exist when the project is complete.
3. Identify the key gaps — each gap should map to at least one task in your plan.
   If a gap has no covering task, either add a task or remove the gap.

CRITICAL wave assignment rule: Tasks that write to ISOLATED directories
(e.g. design-01/, design-02/, ... or feature-a/, feature-b/) with NO
cross-dependencies MUST ALL be the SAME wave. Similar/numbered tasks are
NOT dependent on each other. Sequential waves are ONLY for tasks that
need to read another task's output. Getting this wrong forces serial
execution and wastes enormous time.

Then create the requirements, roadmap, and executable plan based on your expertise.
