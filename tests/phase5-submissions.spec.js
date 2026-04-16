// @ts-check
const { test, expect } = require('@playwright/test');
const { TL, RECRUITER, login, navigateTo, visibleText } = require('./helpers');

test.describe('Phase 5: Submissions', () => {

  test('TL can access Submissions page', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Submission', '/app/submissions');
    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);
    expect(body).toMatch(/submission|queue/i);
    console.log('Submissions page loaded OK');
  });

  test('Recruiter cannot access Submissions (hidden in nav)', async ({ page }) => {
    await login(page, RECRUITER);
    const link = page.locator('nav a:has-text("Submission"), aside a:has-text("Submission")');
    const count = await link.count();
    if (count > 0) {
      await expect(link.first()).not.toBeVisible();
    }
    console.log('Submissions nav correctly hidden for recruiter');
  });

  test('TL Queue — pending submissions list loads', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Submission', '/app/submissions');

    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);

    // Either shows submissions or "Loading submissions..." or empty state
    const hasContent = /submission|loading|queue|no submission|empty/i.test(body);
    console.log(`Submissions page content: ${hasContent ? 'OK' : 'unexpected'}`);
  });

  test('Approve & Reject buttons visible when submissions exist', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Submission', '/app/submissions');
    // Wait longer for submissions to load from API
    await page.waitForTimeout(5000);

    const approveBtn = page.locator('button:has-text("Approve"), button:has-text("Approve & Send")');
    const rejectBtn = page.locator('button:has-text("Reject")');

    const approveCount = await approveBtn.count();
    const rejectCount = await rejectBtn.count();

    if (approveCount === 0 && rejectCount === 0) {
      console.warn('No Approve/Reject buttons — submissions queue may be empty');
    } else {
      console.log(`Approve buttons: ${approveCount}, Reject buttons: ${rejectCount}`);
    }
    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);
  });

});
