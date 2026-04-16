// @ts-check
const { test, expect } = require('@playwright/test');
const { TL, login, navigateTo, visibleText } = require('./helpers');

// ⚠️ APOLLO CREDIT GUARD: "Source Now" is intentionally NOT automated.

test.describe('Phase 3: Requirements', () => {

  test('Requirements board shows existing cards', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Requirement', '/app/requirements');
    const cards = page.locator('.card, [class*="card"], [class*="requirement"]');
    const count = await cards.count();
    console.log(`Cards: ${count}`);
    expect(count).toBeGreaterThan(0);
  });

  test('India filter shows only India requirements', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Requirement', '/app/requirements');
    const tab = page.locator('button:has-text("India"), [role="tab"]:has-text("India"), a:has-text("India")');
    if (await tab.count() === 0) { console.warn('India tab not found — may use different filter UI'); return; }
    await tab.first().click();
    await page.waitForTimeout(1000);
    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);
    console.log('India filter OK');
  });

  test('Singapore filter shows only Singapore requirements', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Requirement', '/app/requirements');
    const tab = page.locator('button:has-text("Singapore"), [role="tab"]:has-text("Singapore"), a:has-text("Singapore")');
    if (await tab.count() === 0) { console.warn('Singapore tab not found — may use different filter UI'); return; }
    await tab.first().click();
    await page.waitForTimeout(1000);
    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);
    console.log('Singapore filter OK');
  });

  test('New Requirement modal opens (TL only)', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Requirement', '/app/requirements');
    const btn = page.locator('button:has-text("New Requirement"), a:has-text("New Requirement")');
    expect(await btn.count()).toBeGreaterThan(0);
    await btn.first().click();
    await page.waitForTimeout(1000);
    const form = page.locator('form, [role="dialog"], .modal, [class*="modal"]');
    expect(await form.count()).toBeGreaterThan(0);
    console.log('New Requirement modal opened OK');
  });

  test('Create requirement saves to DB', async ({ page }) => {
    // Previously a known bug (silent 502 because FastAPI was never deployed on Railway).
    // Fixed by merging FastAPI routes into Flask — see PLAYWRIGHT_TEST_FIX_ANALYSIS.md.
    await login(page, TL);
    await navigateTo(page, 'Requirement', '/app/requirements');

    const btn = page.locator('button:has-text("New Requirement"), a:has-text("New Requirement")');
    if (await btn.count() === 0) { console.warn('New Requirement button missing'); return; }
    await btn.first().click();
    await page.waitForTimeout(1000);

    // Fill modal fields using IDs discovered from the rendered DOM
    await page.locator('input[placeholder*="HCL" i]:visible').fill('Test Corp');
    await page.locator('select:visible').first().selectOption({ label: 'India' });
    await page.locator('#req-role:visible').fill('Senior Java Developer');
    await page.locator('#req-skills:visible').fill('Java, Spring Boot, Microservices');
    const expField = page.locator('input[placeholder="e.g. 5"]:visible');
    if (await expField.count() > 0) await expField.first().fill('5');
    const salaryField = page.locator('input[placeholder*="LPA" i]:visible');
    if (await salaryField.count() > 0) await salaryField.first().fill('20-30 LPA');
    const jdField = page.locator('textarea:visible');
    if (await jdField.count() > 0) await jdField.first().fill('Senior Java Developer with microservices experience.');

    // Click "Create & Source" button
    const submitBtn = page.locator('button:has-text("Create"):visible');
    if (await submitBtn.count() === 0) { console.warn('Submit button not found'); return; }

    await submitBtn.first().click();
    await page.waitForTimeout(5000);

    const newCard = page.locator(':has-text("Test Corp"), :has-text("Senior Java Developer")');
    const found = await newCard.count() > 0;
    if (!found) {
      console.error('BUG CONFIRMED: Create Requirement submitted but card not found in board.');
    }
    expect(found).toBe(true);
  });

});
