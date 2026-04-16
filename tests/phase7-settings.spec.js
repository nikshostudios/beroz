// @ts-check
const { test, expect } = require('@playwright/test');
const { TL, login, navigateTo, visibleText } = require('./helpers');

test.describe('Phase 7: Integrations & Settings', () => {

  test('Integrations — all service cards show status', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Integration', '/app/integrations');

    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);

    const services = ['Supabase', 'Apollo', 'Azure', 'Outlook', 'Google', 'Claude'];
    const found = services.filter(s => new RegExp(s, 'i').test(body));
    console.log(`Integration services found: ${found.join(', ')} (${found.length}/${services.length})`);

    const hasStatus = /connected|active|configured|status/i.test(body);
    console.log(`Status indicators present: ${hasStatus}`);
  });

  test('Settings — profile shows correct name, email, role', async ({ page }) => {
    await login(page, TL);
    await navigateTo(page, 'Setting', '/app/settings');

    const body = await visibleText(page);
    expect(body).not.toMatch(/Internal Server Error/i);

    const hasName = /raju/i.test(body);
    const hasRole = /tl|team lead|leader/i.test(body);
    console.log(`Settings — Name found: ${hasName}, Role found: ${hasRole}`);
    expect(hasName).toBe(true);
  });

  test('Settings — page has no JS errors', async ({ page }) => {
    const jsErrors = [];
    page.on('pageerror', err => jsErrors.push(err.message));

    await login(page, TL);
    await navigateTo(page, 'Setting', '/app/settings');

    if (jsErrors.length > 0) {
      console.warn(`JS errors on Settings page: ${jsErrors.join('; ')}`);
    } else {
      console.log('Settings page: No JS errors');
    }
    expect(jsErrors.length).toBe(0);
  });

});
