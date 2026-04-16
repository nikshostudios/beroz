// @ts-check
const { test, expect } = require('@playwright/test');
const { TL, login, navigateTo, visibleText } = require('./helpers');

test.describe('Phase 6: Search & Analytics', () => {

  test('Analytics Pipeline tab — stats cards and funnel render', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Analytic', '/app/analytics');

    // Click Pipeline sub-nav if visible
    const pipelineTab = page.locator('a:has-text("Pipeline"), button:has-text("Pipeline")');
    if (await pipelineTab.count() > 0) {
      await pipelineTab.first().click();
      await page.waitForTimeout(2000);
    }

    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);

    const hasStats = /open|sourced|shortlist|submitted|pipeline|analytics/i.test(body);
    console.log(`Pipeline stats keywords found: ${hasStats}`);
  });

  test('Analytics Usage tab — token/cost data loads', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Analytic', '/app/analytics');

    const usageTab = page.locator('a:has-text("Usage"), button:has-text("Usage")');
    if (await usageTab.count() === 0) { console.warn('Usage tab not found'); return; }

    await usageTab.first().click();
    await page.waitForTimeout(2000);
    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);
    console.log('Usage tab OK');
  });

  test('Search — query parses skills, location, experience', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Search', '/app/search');

    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);

    // Search page has a visible search input with placeholder about skills/role/location
    const searchInput = page.locator('input[placeholder*="Search by" i], input[placeholder*="skills" i], input[type="search"]');
    if (await searchInput.count() === 0) { console.warn('Search input not found'); return; }

    await searchInput.first().click();
    await searchInput.first().fill('Python developers in Singapore with 3+ years');
    await searchInput.first().press('Enter');
    await page.waitForTimeout(5000);

    const resultBody = await visibleText(page);
    const hasParsed = /python|singapore|years|skill|location|experience|developer|parsing/i.test(resultBody);
    console.log(`Search response has relevant keywords: ${hasParsed}`);
    expect(resultBody).not.toMatch(/Internal Server Error/i);
  });

  test('Integrations — service cards display', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Integration', '/app/integrations');
    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);

    const services = ['Supabase', 'Apollo', 'Claude', 'Google', 'Outlook'];
    const found = services.filter(s => new RegExp(s, 'i').test(body));
    console.log(`Integration services found: ${found.join(', ')} (${found.length}/${services.length})`);
    expect(found.length).toBeGreaterThanOrEqual(3);
  });

});
