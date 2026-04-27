"""Prompt templates for the planning pipeline.

Each constant is a format string.  The pipeline fills in context variables
(description, interview_transcript, research files, etc.) before passing
the prompt to a task.
"""

# ======================================================================
# Interview prompts
# ======================================================================

_INTERVIEW_OUTPUT_FLOW = """
## OUTPUT FLOW — follow these steps exactly

### STEP 1: Present a readable summary (NO JSON)

When you have enough context, present the plan as a **readable markdown summary**:

## Project Summary
One paragraph describing the project.

## Requirements
- **REQ-01** (must): Description...
- **REQ-02** (should): Description...

## Phases
### Phase 1: Name
**Goal:** What this phase delivers
**Success criteria:** Observable outcomes
**Tasks:**
1. [wave 1, implementer] Task description
2. [wave 2, qa-lead] Another task description

### Phase 2: Name
...

### Plan structure guidelines

- **Right-size your phases.** Each phase should be a coherent unit of work that
  can be reviewed on its own. A phase that builds 4 independent HTML files is
  fine if they're parallel. A phase that builds the backend, frontend, AND tests
  is too large — split it.
- **Separate building from verifying.** If the project has significant complexity,
  add review or QA phases after implementation phases. Don't cram implementation
  and testing into the same phase unless the work is trivial.
- **Use waves within phases for parallelism.** Tasks in the same wave run
  concurrently. Tasks that edit the same file MUST be in different waves.
  Independent files (e.g. 4 separate HTML pages) CAN be in the same wave.
- **3-8 phases is typical.** A single-file script might be 2 phases. A multi-page
  app with backend should be 5-8. If you only have 2 phases for a non-trivial
  project, you're probably under-decomposing.
- **Each phase needs testable success criteria.** These must be specific enough
  that a reviewer can verify them by inspecting code with Read, Glob, and Grep.
  BAD: "Authentication works." GOOD: "auth.py exports register() and login();
  passwords are hashed; tests in test_auth.py pass."
- **Wave 1 = foundation, Wave 2+ = what depends on it.** Put project skeleton,
  config, database schema, and base templates in wave 1. Tasks that import or
  extend wave 1's output go in wave 2+. Independent files can share a wave.
- **Write actionable task prompts.** Each task prompt should specify what to
  build, which files to create/modify, and how to verify it worked.
  BAD: "Set up the backend." GOOD: "Create src/server.ts with Express, add
  GET /health endpoint returning {{status:'ok'}}. Install express and cors."
- **Front-load risk.** Put the riskiest or most uncertain work in early phases.
  If an external API integration or complex algorithm might not work, discover
  that in Phase 1, not Phase 5.
- **Define interfaces between phases.** If Phase 2 depends on Phase 1, the
  Phase 1 success criteria must include the exact contracts (routes, schemas,
  function signatures) that Phase 2 will consume.

Then ask exactly: "Does this plan look good? Reply **yes** to confirm, or tell me what to change."

### STEP 2: Wait for confirmation

If the user requests changes, revise and re-present the readable summary.
Do NOT show any JSON during revision — only the readable summary.

### STEP 3: After explicit confirmation, emit JSON

Only after the user explicitly confirms (yes, looks good, confirmed, approve,
go ahead, etc.), output the SAME plan as JSON in a ```json code fence using
this EXACT schema:

```json
{{
    "project_summary": "One paragraph describing the project",
    "requirements": [
        {{"id": "REQ-01", "text": "...", "priority": "must"}},
        {{"id": "REQ-02", "text": "...", "priority": "should"}}
    ],
    "phases": [
        {{
            "name": "Phase name",
            "goal": "What this phase delivers",
            "success_criteria": ["Observable outcome 1", "Observable outcome 2"],
            "requirements": ["REQ-01", "REQ-02"],
            "tasks": [
                {{"prompt": "Detailed task instructions...", "wave": 1, "expert": "implementer-general"}},
                {{"prompt": "Another task...", "wave": 2, "expert": "qa-lead"}}
            ]
        }}
    ]
}}
```

{expert_options}
Wave numbers control parallel execution — tasks in the same wave run concurrently.
Tasks writing to ISOLATED directories (design-01/, design-02/, feature-a/, feature-b/)
with NO cross-dependencies MUST ALL be the SAME wave. Similar/numbered tasks are NOT
dependent. Sequential waves are ONLY for tasks that read another task's output.

Immediately after the JSON fence, output this marker on its own line:

===CONFIRMED===

## CRITICAL RULES

1. You are a PLANNER only. Do NOT create files or write code. You MAY use
   read-only tools (Read, Glob, Grep, WebFetch) to inspect existing content,
   and you MAY invoke Skills (e.g. ui-ux-pro-max for web design projects).
   Your primary job is to have a conversation and output a plan.
2. NEVER show JSON until the user has explicitly confirmed the readable summary.
3. NEVER mention ===CONFIRMED=== to the user — it is an internal pipeline signal.
4. The JSON must exactly match the approved summary — no adding or removing items.
5. Do NOT output ===CONFIRMED=== until the user has explicitly approved.
"""

SIMPLE_INTERVIEW_PROMPT = """\
The user described their project as:
"{description}"

Have a short conversation (3-5 questions) to clarify the most important details:
- What is the core problem this solves and who is it for?
- What are the must-have features? Roughly how many screens/pages/endpoints?
- Tech stack preferences? Where will this run (browser, server, CLI, mobile)?
- Does it need user auth or different user roles?
- Is there existing code, APIs, or data sources to integrate with?
""" + _INTERVIEW_OUTPUT_FLOW

DEEP_INTERVIEW_PROMPT = """\
The user described their project as:
"{description}"

Your goal is to extract a complete mental model of what they want to build.

Ask 10-15 questions across these dimensions, adapting based on answers:

**Motivation & Context**
- Why does this need to exist? What's the trigger?
- Who is this for? (Be specific — "developers" is too vague)
- What happens today without this? What's the pain?

**Scope & Boundaries**
- What's the smallest version that would be useful?
- What are you explicitly NOT building? (Anti-scope)
- Are there hard constraints? (Timeline, budget, platform, compliance)

**Concreteness**
- Walk me through a user's typical session from start to finish
- What does the main screen / interface look like?
- What data flows in and out?

**Technical**
- Any existing code, APIs, or systems this must integrate with?
- Tech stack preferences or requirements?
- Deployment environment?

**Definition of Done**
- How will you know v1 is "done"?
- What's the one thing that MUST work perfectly?
- What can be rough/manual in v1?
""" + _INTERVIEW_OUTPUT_FLOW


# ======================================================================
# Research prompts — persona lives in expert .md files, prompts carry data only
# ======================================================================

_RESEARCHER_CONTEXT = """\
A project planning interview has been conducted.

<project_description>
{description}
</project_description>

<interview_transcript>
{interview}
</interview_transcript>

Research this project based on your expertise. Be specific to this project's needs.
"""

RESEARCHER_STACK_PROMPT = _RESEARCHER_CONTEXT
RESEARCHER_FEATURES_PROMPT = _RESEARCHER_CONTEXT
RESEARCHER_ARCHITECTURE_PROMPT = _RESEARCHER_CONTEXT
RESEARCHER_PITFALLS_PROMPT = _RESEARCHER_CONTEXT


# ======================================================================
# Synthesis prompt
# ======================================================================

SYNTHESIZER_PROMPT = """\
Four parallel researchers have investigated different aspects of a project.

<project_description>
{description}
</project_description>

<interview_transcript>
{interview}
</interview_transcript>

<stack_research>
{research_stack}
</stack_research>

<features_research>
{research_features}
</features_research>

<architecture_research>
{research_architecture}
</architecture_research>

<pitfalls_research>
{research_pitfalls}
</pitfalls_research>

Synthesize the research into a unified document the roadmapper can act on.
Use the interview transcript to verify researcher claims against what the user
actually said — flag any recommendation that contradicts stated constraints or
adds scope the user did not request.

## Synthesis rules

1. **RESOLVE CONTRADICTIONS** — When researchers disagree (e.g., different tech
   recommendations), state both sides, evaluate trade-offs for THIS project,
   and make a clear recommendation.
2. **PRIORITIZE BY IMPACT** — Lead with findings that affect architecture and
   phase ordering. A database choice matters more than a CSS framework choice.
3. **DE-DUPLICATE** — Merge overlapping findings into one item with the
   strongest evidence.
4. **FLAG WEAK RESEARCH** — If a researcher produced generic advice not specific
   to this project, say so. The roadmapper needs to know what to trust.
5. **CONNECT TO DECISIONS** — For each key finding, state the decision it
   implies: "Use PostgreSQL → Phase 1 must include DB setup and migrations."
6. **PRESERVE SPECIFICS** — Keep version numbers, library names, API endpoints,
   and configuration details. The roadmapper will lose these if you abstract
   them away. Write "Express 4.x with cors middleware" not "a web framework."
"""


# ======================================================================
# Roadmapper prompt
# ======================================================================

ROADMAPPER_PROMPT = """\
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

{expert_options}

Before writing requirements, perform a brief state-gap analysis:
1. List what currently exists (files, capabilities, infrastructure — whatever
   dimensions are relevant to this project). If this is a greenfield project,
   state that explicitly.
2. List what must exist when the project is complete.
3. Identify the key gaps — each gap should map to at least one task in your plan.
   If a gap has no covering task, either add a task or remove the gap.

Then create the requirements, roadmap, and executable plan based on your expertise.
"""


# ======================================================================
# Plan checker prompt
# ======================================================================

PLAN_CHECKER_PROMPT = """\
<interview_transcript>
{interview}
</interview_transcript>

<requirements>
{requirements}
</requirements>

<roadmap>
{roadmap}
</roadmap>

<planned_phases>
{plan}
</planned_phases>

Verify this plan based on your expertise. Use the interview transcript to check
that the plan actually addresses what the user asked for — not just that it is
internally consistent.

If the requirements section contains a State Analysis with Key Gaps, verify that
every listed gap is addressed by at least one task in the plan. Flag any uncovered
gap as CRITICAL — a gap with no covering task means the plan will not achieve the
goal state.
"""


# ======================================================================
# Roadmapper revision prompt (fed checker issues back)
# ======================================================================

ROADMAPPER_REVISION_PROMPT = """\
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

Revise the plan to address the issues. Do NOT drop correct content -- only fix what was flagged.
"""


# ======================================================================
# Per-phase prompts
# ======================================================================

DISCUSS_TASK_PROMPT = """\
<task_info>
Task: {task_name}
Expert: {task_expert}
Wave: {task_wave}
Prompt: {task_prompt}
</task_info>

<project_context>
{project_context}
</project_context>

## Output format

After the conversation, output your findings in this format after a
===CONTEXT=== marker:

===CONTEXT===
## Decisions (Locked)
- [Decision 1]: [User's choice and rationale]
- [Decision 2]: [User's choice and rationale]

## Claude's Discretion
- [Area 1]: [What Claude can decide freely]

## Deferred Ideas
- [Idea 1]: [Noted but out of scope]

## IMPORTANT: Confirmation flow

After presenting your summary of decisions, ask the user to confirm:
"Does this capture everything correctly? Reply **yes** to confirm."

Only after the user explicitly confirms, output the final context document
after the ===CONTEXT=== marker, followed by this marker on its own line:

===CONFIRMED===

## CRITICAL RULES

1. You MUST ask the user at least one question before producing output.
   NEVER go straight to ===CONTEXT=== on your first message.
2. NEVER output ===CONFIRMED=== until the user has explicitly approved.
3. NEVER mention ===CONFIRMED=== to the user — it is an internal signal.
4. If there are genuinely no gray areas, tell the user that and ask if
   they have any preferences they want to lock before implementation.
"""

RESEARCH_TASK_PROMPT = """\
<task_info>
Task: {task_name}
Expert: {task_expert}
Prompt: {task_prompt}
</task_info>

<project_context>
{project_context}
</project_context>

Research the specific domain concerns for THIS task only.
Use your methodology to investigate and produce your standard output document.

If the project context includes locked decisions (in <task_decisions>),
those are NON-NEGOTIABLE — research within those constraints.

Output ONLY the markdown document content. Do not write files.
"""


# ======================================================================
# Phase review prompt (post-phase reviewer task)
# ======================================================================

PHASE_REVIEW_PROMPT = """\
Review the completed work for Phase {phase_number}: {phase_name}.

## Phase Goal
{phase_goal}

## Working Directory
{working_dir}

Use Read, Glob, and Grep tools to inspect the codebase in the working directory.
Check the work against the phase goal above. Do NOT modify any files.

IMPORTANT:
- Only flag issues that are actual defects in THIS phase's deliverables.
  Do NOT flag forward scaffolding intended for later phases.
- If the phase goal includes success criteria or requirements, check EVERY
  one of them systematically — do not stop after finding the first issue.
- If the CRITICAL section has no issues, write "None found." under it.

Provide your review using EXACTLY this format:

### Review: Phase {phase_number} — {phase_name}

#### CRITICAL (must fix before next phase)
Any success criterion that is NOT met is CRITICAL. Also flag:
bugs, security issues, broken contracts, or missing functionality.
Judge the phase AS DELIVERED — do not assume a later phase will fix it.
CRITICALs BLOCK the next phase from starting.

#### WARNING (should fix)
Issues that don't violate success criteria but reduce quality:
edge cases, performance concerns, error handling gaps, missing tests for
non-trivial logic, poor code organization, significant duplication,
inconsistent API design, missing input validation.
Warnings are passed to the next phase as context.

#### NIT (optional)
Minor improvements, style suggestions.

#### Summary
State what you verified and your overall assessment.
"""


# ======================================================================
# Phase review fix prompt (auto-fix task for CRITICALs)
# ======================================================================

PHASE_REVIEW_FIX_PROMPT = """\
The code review for Phase {phase_number}: {phase_name} found CRITICAL issues \
that must be fixed.

## Phase Goal
{phase_goal}

## Working Directory
{working_dir}

## CRITICAL Issues to Fix
{critical_issues}

## Full Review
{review_text}

Fix ALL the CRITICAL issues listed above. You may use Read, Glob, Grep, \
Write, Edit, and Bash tools.

IMPORTANT:
- Understand the root cause before making changes
- After fixing each issue, verify the fix doesn't break other \
requirements or introduce new problems
- Check that your changes are consistent with the rest of the codebase
- Run the project's test suite after fixing to verify no regressions
- At the end, list what you changed, what tests you ran, and what you \
verified still works
"""


# ======================================================================
# Consult chat prompts (advisory sidebar — no tool use)
# ======================================================================

CONSULT_TASK_PROMPT = """\
{persona}

## Task context

- **Task name**: {task_name}
- **Expert persona**: {expert}
- **Current status**: {status}

## Task prompt (what the task was asked to do)

{task_prompt}

## Recent task output (what Claude has said so far)

{recent_output}

Do not use any tools — your response must be plain text advice only.
"""

CONSULT_PROJECT_PROMPT = """\
{persona}

## Current form values

- **Project name**: {name}
- **Description**: {description}
- **Working directory**: {working_dir}
- **Auto-plan**: {auto_plan}
- **Interview depth**: {interview_mode}
- **Research enabled**: {research}
- **Verification enabled**: {verification}
- **Phase review enabled**: {phase_review}

Do not use any tools — your response must be plain text advice only.
"""
