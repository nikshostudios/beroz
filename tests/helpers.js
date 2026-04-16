// Shared test helpers
const BASE = 'https://exceltechcomputers.up.railway.app';
const TL = { user: 'raju', pass: 'raju18' };
const RECRUITER = { user: 'devesh', pass: 'devesh27' };

async function login(page, { user, pass }) {
  await page.goto('/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  // Wait for the login form to actually render
  const usernameField = page.locator('input[name="username"], #username, input[placeholder*="sername" i]');
  await usernameField.waitFor({ state: 'visible', timeout: 20000 });
  await usernameField.fill(user);
  await page.fill('input[type="password"], #password', pass);
  await page.click('button[type="submit"], button:has-text("Login"), button:has-text("Sign in")');
  await page.waitForURL(url => !url.href.includes('/login') && url.pathname !== '/', { timeout: 30000 });
}

async function navigateTo(page, navText, fallbackPath) {
  const link = page.locator(`nav a:has-text("${navText}"), aside a:has-text("${navText}")`);
  if (await link.count() > 0) {
    await link.first().click();
  } else {
    await page.goto(`${BASE}${fallbackPath}`);
  }
  await page.waitForLoadState('domcontentloaded', { timeout: 20000 });
  await page.waitForTimeout(2000);
}

// Get visible page text (excludes <script> content, unlike textContent)
async function visibleText(page) {
  return await page.innerText('body');
}

// Assert page has no server errors in visible text
async function assertNoServerError(page, expect) {
  const text = await visibleText(page);
  expect(text).not.toMatch(/Internal Server Error/i);
}

module.exports = { BASE, TL, RECRUITER, login, navigateTo, visibleText, assertNoServerError };
