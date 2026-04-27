/**
 * E2E test: test1 pipeline flow
 *
 * Tests the full pipeline: create project → interview (W1) → researcher (W2) → file_writer (W3)
 * Verifies the file_writer produces full (non-truncated) output.
 *
 * Prerequisites: server running on localhost:8080, "test1" pipeline template exists.
 */
import { test, expect } from '@playwright/test';
import Database from 'better-sqlite3';
import * as fs from 'fs';
import * as path from 'path';

const DB_PATH = path.resolve('Z:/py-orchestrator/orchestrator.db');
const PROJECT_NAME = `e2e-${Date.now()}`;
const WORKING_DIR = `//vmware-host/Shared Folders/hosthdd/z/${PROJECT_NAME}`;

/** Query the SQLite DB directly via better-sqlite3. */
function dbQuery(sql: string): any[] {
  const db = new Database(DB_PATH, { readonly: true });
  try {
    return db.prepare(sql).all();
  } finally {
    db.close();
  }
}

/** Poll DB until condition is met. */
async function pollDB(
  sql: string,
  check: (rows: any[]) => boolean,
  intervalMs = 3000,
  maxMs = 180_000,
): Promise<any[]> {
  const start = Date.now();
  while (Date.now() - start < maxMs) {
    const rows = dbQuery(sql);
    if (check(rows)) return rows;
    await new Promise(r => setTimeout(r, intervalMs));
  }
  throw new Error(`pollDB timed out after ${maxMs}ms`);
}

/** Wait for a specific task to reach a target status. */
async function waitForStatus(
  taskId: string,
  targetStatus: string,
  intervalMs = 3000,
  maxMs = 120_000,
): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < maxMs) {
    const rows = dbQuery(`SELECT status FROM tasks WHERE id='${taskId}'`);
    if (rows.length && rows[0].status === targetStatus) return;
    // If already completed/failed, stop waiting
    if (rows.length && ['completed', 'failed', 'cancelled'].includes(rows[0].status)) return;
    await new Promise(r => setTimeout(r, intervalMs));
  }
  throw new Error(`waitForStatus('${taskId}', '${targetStatus}') timed out`);
}

const tasksSql = () =>
  `SELECT id, status, name, wave FROM tasks WHERE project_id=(SELECT id FROM projects WHERE name='${PROJECT_NAME}') ORDER BY wave, created_at`;

test.describe('test1 pipeline flow', () => {

  test('create project, complete interview, verify full file output', async ({ page }) => {

    // ── Step 1: Create project ──────────────────────────────────
    await test.step('create project', async () => {
      await page.goto('/projects');
      await page.getByRole('textbox', { name: 'Project Name' }).fill(PROJECT_NAME);
      await page.getByRole('textbox', { name: 'Working Directory' }).fill(WORKING_DIR);
      await page.getByRole('textbox', { name: 'Description' }).fill('A CLI calculator app in Python');
      await page.getByRole('checkbox', { name: /Create directory/i }).setChecked(true);
      await page.getByLabel('Pipeline Template').selectOption('Project Planner');
      await page.getByRole('button', { name: 'Create Project' }).click();
      await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT_NAME}`));
    });

    // ── Step 2: Wait for interview to reach awaiting_input ──────
    let interviewId: string;
    await test.step('wait for interview awaiting_input', async () => {
      const tasks = await pollDB(tasksSql(), (rows) =>
        rows.some(r => r.name === 'Interview' && r.status === 'awaiting_input')
      );
      const interview = tasks.find(r => r.name === 'Interview')!;
      expect(interview.wave).toBe(1);
      interviewId = interview.id;
    });

    // ── Step 3: Reply loop — send plan, then confirm until completed ──
    await test.step('reply to interview until completed', async () => {
      const replies = [
        // First reply: strong instruction to skip questions and present plan
        "Stop asking questions. Here is everything you need. " +
        "Project: CLI calculator in Python. " +
        "Requirements: REQ-01 (must) app.py with add/subtract/multiply/divide functions. " +
        "REQ-02 (must) test_app.py with pytest tests for all 4 ops including divide-by-zero. " +
        "Single phase, 2 waves: wave 1 implementer builds app.py, wave 2 qa-lead builds test_app.py. " +
        "Present this as the plan summary NOW and ask for confirmation.",
        // Subsequent replies: just confirm
        "yes", "yes", "yes",
      ];

      for (const reply of replies) {
        // Check current status
        const rows = dbQuery(`SELECT status FROM tasks WHERE id='${interviewId}'`);
        if (!rows.length) break;
        const status = rows[0].status;

        // If already completed, we're done
        if (['completed', 'failed', 'cancelled'].includes(status)) break;

        // Wait for awaiting_input if currently running
        if (status === 'running') {
          await waitForStatus(interviewId, 'awaiting_input');
          const recheck = dbQuery(`SELECT status FROM tasks WHERE id='${interviewId}'`);
          if (recheck[0]?.status === 'completed') break;
        }

        // Navigate and send reply
        await page.goto(`/tasks/${interviewId}`);
        const replyBox = page.getByRole('textbox', { name: /Type your reply/i });
        await expect(replyBox).toBeVisible({ timeout: 10_000 });
        await replyBox.fill(reply);
        await page.getByRole('button', { name: 'Send' }).click();

        // Wait for task to leave awaiting_input (go to running or completed)
        await new Promise(r => setTimeout(r, 2000));
      }

      // Final wait: interview must complete
      await waitForStatus(interviewId, 'completed', 3000, 120_000);
      const finalRows = dbQuery(`SELECT status FROM tasks WHERE id='${interviewId}'`);
      expect(finalRows[0].status).toBe('completed');
    });

    // ── Step 4: Wait for phase to complete (includes instant nodes like file_writer) ──
    let allTasks: any[];
    await test.step('wait for pipeline completion', async () => {
      // Wait for phase status = complete AND all tasks terminal.
      // Phase 'complete' fires after all waves (including file_writer in Wave 3).
      const combinedSql = `
        SELECT
          (SELECT status FROM phases WHERE project_id=(SELECT id FROM projects WHERE name='${PROJECT_NAME}') LIMIT 1) as phase_status,
          (SELECT COUNT(*) FROM tasks WHERE project_id=(SELECT id FROM projects WHERE name='${PROJECT_NAME}') AND status NOT IN ('completed','failed','cancelled')) as active_tasks,
          (SELECT COUNT(*) FROM tasks WHERE project_id=(SELECT id FROM projects WHERE name='${PROJECT_NAME}')) as total_tasks
      `;
      await pollDB(combinedSql, (rows) =>
        rows.length > 0
        && rows[0].phase_status === 'complete'
        && rows[0].active_tasks === 0
        && rows[0].total_tasks >= 2
      , 5000, 180_000);

      allTasks = dbQuery(tasksSql());
      const interview = allTasks.find(r => r.wave === 1)!;
      const researcher = allTasks.find(r => r.wave === 2)!;
      expect(interview.status).toBe('completed');
      expect(researcher.status).toBe('completed');
    });

    // ── Step 5: Verify cauw.MD has FULL content ─────────────────
    await test.step('verify cauw.MD is full (not truncated)', async () => {
      const researcher = allTasks!.find(r => r.wave === 2)!;
      const filePath = path.join(WORKING_DIR, '.orchestrator', 'research', 'cauw.MD');
      expect(fs.existsSync(filePath)).toBe(true);

      // Normalize line endings (Windows write_text converts \n → \r\n)
      const normalize = (s: string) => s.replace(/\r\n/g, '\n');
      const fileContent = normalize(fs.readFileSync(filePath, 'utf-8'));

      // Get full result from task_outputs result event
      const resultRows = dbQuery(
        `SELECT content FROM task_outputs WHERE task_id='${researcher.id}' AND event_type='result' LIMIT 1`
      );
      expect(resultRows.length).toBe(1);

      const resultEvent = typeof resultRows[0].content === 'string'
        ? JSON.parse(resultRows[0].content)
        : resultRows[0].content;
      const fullResult = normalize(resultEvent.result);

      // KEY ASSERTION: file matches full result, not truncated summary
      expect(fileContent.length).toBe(fullResult.length);
      expect(fileContent).toBe(fullResult);
      expect(fileContent.length).toBeGreaterThan(2000); // exceeds truncation limit

      // Verify truncated summary is shorter
      const summaryRows = dbQuery(
        `SELECT result_summary FROM tasks WHERE id='${researcher.id}'`
      );
      const summary = summaryRows[0].result_summary;
      expect(summary.length).toBeLessThanOrEqual(2000);
      expect(fileContent.length).toBeGreaterThan(summary.length);
    });

    // ── Step 6: Verify project page shows complete ──────────────
    await test.step('verify project page', async () => {
      await page.goto(`/projects/${PROJECT_NAME}`);
      await expect(page.getByText('complete', { exact: true })).toBeVisible();

      // No mermaid DAG diagram
      const dagCount = await page.locator('.phase-dag').count();
      expect(dagCount).toBe(0);
    });
  });
});
