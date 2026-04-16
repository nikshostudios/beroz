// @ts-check
const { test, expect } = require('@playwright/test');
const { TL, login, navigateTo, visibleText } = require('./helpers');

// Phase 4: Outreach & Email
// Most tests here expect graceful errors since Azure/Outlook is not configured.

test.describe('Phase 4: Outreach & Email', () => {

  test('Outreach page loads without server error', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Sequence', '/app/outreach');
    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);
    expect(body).toMatch(/outreach|inbox|sequence/i);
    console.log('Outreach page loaded OK');
  });

  test('Check Inbox — graceful error when Azure not configured', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Sequence', '/app/outreach');

    // Select a recruiter if dropdown exists
    const recruiterSelect = page.locator('select');
    if (await recruiterSelect.count() > 0) {
      await recruiterSelect.first().selectOption({ index: 1 });
    }

    const checkBtn = page.locator('button:has-text("Check Inbox")');
    if (await checkBtn.count() === 0) {
      console.warn('Check Inbox button not found');
      return;
    }

    await checkBtn.first().click();
    await page.waitForTimeout(5000);
    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);
    const hasResponse = /error|not configured|azure|email|inbox|no emails/i.test(body);
    console.log(`Check Inbox response: ${hasResponse ? 'has feedback' : 'silent'}`);
  });

  test('Send Outreach tab — file upload inputs present', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Sequence', '/app/outreach');

    const sendTab = page.locator('a:has-text("Send Outreach"), button:has-text("Send Outreach")');
    if (await sendTab.count() > 0) {
      await sendTab.first().click();
      await page.waitForTimeout(1000);
    }

    const fileInput = page.locator('input[type="file"]');
    const count = await fileInput.count();
    console.log(`File inputs found: ${count}`);
    expect(count).toBeGreaterThan(0);
  });

});
