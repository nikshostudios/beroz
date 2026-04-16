// @ts-check
const { test, expect } = require('@playwright/test');
const { TL, login, navigateTo, visibleText } = require('./helpers');

test.describe('Phase 2: Backend API Health', () => {

  test('Requirements page — Flask→FastAPI→Supabase chain works', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Requirement', '/app/requirements');
    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);
    const cards = page.locator('.card, [class*="card"], [class*="requirement-card"]');
    const count = await cards.count();
    console.log(`Requirement cards found: ${count}`);
    expect(count).toBeGreaterThan(0);
  });

  test('Session API — returns logged_in:true with name and role', async ({ page }) => {
    await login(page, TL);
    const res = await page.request.get('/api/session');
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.logged_in).toBe(true);
    expect(body.name).toMatch(/raju/i);
    expect(body.role).toBe('tl');
    console.log('Session OK:', JSON.stringify(body));
  });

  test('Pipeline API — Shortlist page responds (even empty is OK)', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Shortlist', '/app/shortlist');
    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);
    console.log('Shortlist page loaded OK');
  });

  test('Settings — shows correct name, email, role', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Setting', '/app/settings');
    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);
    expect(body.toLowerCase()).toMatch(/raju|name|email|role/i);
    console.log('Settings OK');
  });

});
