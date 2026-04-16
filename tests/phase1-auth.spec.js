// @ts-check
const { test, expect } = require('@playwright/test');

const BASE = 'https://exceltechcomputers.up.railway.app';
const TL = { user: 'raju', pass: 'raju18' };
const RECRUITER = { user: 'devesh', pass: 'devesh27' };

async function login(page, { user, pass }) {
  await page.goto('/');
  await page.fill('input[name="username"], input[placeholder*="username" i], #username', user);
  await page.fill('input[name="password"], input[type="password"], #password', pass);
  await page.click('button[type="submit"], input[type="submit"], button:has-text("Login"), button:has-text("Sign in")');
  await page.waitForURL(/\/(app|dashboard|home|agent)/, { timeout: 10000 });
}

// ─── Phase 1: Auth & Navigation ───────────────────────────────────────────────

test.describe('Phase 1: Auth & Navigation', () => {

  test('Login as TL (raju) → redirects to Agent Home', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL(/\//);

    // Fill login form
    await page.fill('input[name="username"], input[placeholder*="username" i], #username', TL.user);
    await page.fill('input[name="password"], input[type="password"], #password', TL.pass);
    await page.click('button[type="submit"], input[type="submit"], button:has-text("Login"), button:has-text("Sign in")');

    // Should redirect away from login (waitForURL callback receives a URL object)
    await page.waitForURL(url => !url.href.includes('/login') && url.pathname !== '/', { timeout: 10000 });
    console.log('Redirected to:', page.url());
  });

  test('Session API returns logged_in:true with TL role', async ({ page }) => {
    await login(page, TL);

    const response = await page.request.get('/api/session');
    expect(response.status()).toBe(200);
    const body = await response.json();
    console.log('Session:', JSON.stringify(body));
    expect(body.logged_in).toBe(true);
    expect(body.role).toBe('tl');
    expect(body.name).toBeTruthy();
  });

  test('All sidebar nav pages load without JS errors', async ({ page }) => {
    const jsErrors = [];
    page.on('pageerror', err => jsErrors.push(err.message));

    await login(page, TL);

    // Collect nav links from the sidebar
    const navLinks = await page.$$eval(
      'nav a[href], aside a[href], .sidebar a[href]',
      links => links.map(a => ({ href: a.getAttribute('href'), text: a.textContent?.trim() }))
    );

    console.log('Nav links found:', navLinks.length, navLinks.map(l => l.text).join(', '));
    expect(navLinks.length).toBeGreaterThan(0);

    for (const link of navLinks) {
      if (!link.href || link.href.startsWith('#') || link.href.startsWith('javascript')) continue;
      const url = link.href.startsWith('http') ? link.href : `${BASE}${link.href}`;
      await page.goto(url);
      await page.waitForLoadState('domcontentloaded');
      console.log(`  ✓ ${link.text} → ${page.url()} (${jsErrors.length} JS errors so far)`);
    }

    if (jsErrors.length > 0) {
      console.warn('JS errors encountered:', jsErrors);
    }
    expect(jsErrors.length).toBe(0);
  });

  test('TL-only features visible: Submissions nav + New Requirement button', async ({ page }) => {
    await login(page, TL);

    // Check Submissions is in nav
    const submissionsNav = page.locator('nav a:has-text("Submission"), aside a:has-text("Submission"), .sidebar a:has-text("Submission")');
    await expect(submissionsNav.first()).toBeVisible({ timeout: 5000 });

    // Check New Requirement button exists somewhere on page
    const newReqBtn = page.locator('button:has-text("New Requirement"), a:has-text("New Requirement")');
    // Navigate to requirements page first
    await page.goto(`${BASE}/app`);
    const reqLink = page.locator('nav a:has-text("Requirement"), aside a:has-text("Requirement")');
    if (await reqLink.count() > 0) {
      await reqLink.first().click();
      await page.waitForLoadState('domcontentloaded');
    }
    await expect(newReqBtn.first()).toBeVisible({ timeout: 5000 });
  });

  test('Logout → redirects to login page', async ({ page }) => {
    await login(page, TL);

    const logoutBtn = page.locator('a:has-text("Logout"), button:has-text("Logout"), a:has-text("Sign out"), button:has-text("Sign out")');
    await logoutBtn.first().click();

    // Should return to login/root
    await page.waitForURL(url => url.pathname === '/' || url.href.includes('login'), { timeout: 8000 });
    console.log('After logout:', page.url());
  });

  test('Recruiter (devesh) — Submissions nav hidden, no New Requirement button', async ({ page }) => {
    await login(page, RECRUITER);

    // Submissions nav should NOT be visible for recruiter role
    // Note: element may exist in DOM but hidden via CSS — check visibility, not count
    const submissionsNav = page.locator('nav a:has-text("Submission"), aside a:has-text("Submission"), .sidebar a:has-text("Submission")');
    const count = await submissionsNav.count();
    if (count > 0) {
      // If element exists, it must be hidden
      await expect(submissionsNav.first()).not.toBeVisible();
    }
    // else: not in DOM at all — also passes

    // New Requirement button should NOT be visible for recruiter role
    const newReqBtn = page.locator('button:has-text("New Requirement"), a:has-text("New Requirement")');
    const btnCount = await newReqBtn.count();
    if (btnCount > 0) {
      await expect(newReqBtn.first()).not.toBeVisible();
    }
  });

  test('Landing page /home — SaaS and ExcelTech product cards display', async ({ page }) => {
    await page.goto('/home');
    await page.waitForLoadState('domcontentloaded');

    const bodyText = await page.textContent('body');
    console.log('Page title:', await page.title());

    // Check for product card content
    expect(bodyText?.toLowerCase()).toMatch(/saas|juicebox/i);
    expect(bodyText?.toLowerCase()).toMatch(/exceltech|excel tech|recruitment/i);
  });

});

// ─── Phase 7: Settings (works without external services) ──────────────────────

test.describe('Phase 7: Settings', () => {

  test('Settings page shows correct name, email, role for TL', async ({ page }) => {
    await login(page, TL);

    // Navigate to settings
    const settingsLink = page.locator('nav a:has-text("Setting"), aside a:has-text("Setting"), a[href*="setting"]');
    if (await settingsLink.count() > 0) {
      await settingsLink.first().click();
      await page.waitForLoadState('domcontentloaded');
    } else {
      await page.goto(`${BASE}/app/settings`);
    }

    const bodyText = await page.textContent('body');
    console.log('Settings page loaded:', page.url());

    // Should show TL user info somewhere
    expect(bodyText?.toLowerCase()).toMatch(/raju|team lead|tl/i);
  });

});
