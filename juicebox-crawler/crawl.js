/**
 * Juicebox DOM Crawler
 *
 * Systematically crawls every page/tab in Juicebox and saves
 * the live DOM as HTML files for UI cloning.
 *
 * Usage:
 *   node crawl.js
 *
 * It will open a browser, pause for you to login with Google,
 * then automatically crawl every page.
 */

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

// === CONFIG ===
const BASE_URL = 'https://app.juicebox.ai';
const START_URL = 'https://app.juicebox.ai/project/azOIT7xZWnbDRh7RHxuA/agent';
const OUTPUT_DIR = path.join(__dirname, 'html-captures');
const SESSION_DIR = path.join(__dirname, 'auth-session');
const WAIT_MS = 3000; // Wait for page to fully render after navigation
const MAX_DEPTH = 3;  // How deep to crawl nested views

// Track what we've already captured
const visitedUrls = new Set();
const visitedButtons = new Set();
let captureCount = 0;

// Create output directory
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}

/**
 * Sanitize a string for use as a filename
 */
function toFilename(text) {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .substring(0, 60);
}

/**
 * Save the current page's DOM to an HTML file
 */
async function saveDom(page, name) {
  captureCount++;
  const filename = `${String(captureCount).padStart(3, '0')}-${toFilename(name)}.html`;
  const filepath = path.join(OUTPUT_DIR, filename);

  // Wait for any animations/loading to settle
  await page.waitForTimeout(WAIT_MS);

  // Get the full rendered DOM
  const html = await page.evaluate(() => document.documentElement.outerHTML);

  // Also capture the current URL and page title for reference
  const url = page.url();
  const title = await page.title();

  // Add a comment at the top with metadata
  const metadata = `<!--
  Captured: ${new Date().toISOString()}
  URL: ${url}
  Title: ${title}
  Name: ${name}
-->\n`;

  fs.writeFileSync(filepath, metadata + html, 'utf-8');
  console.log(`  ✅ Saved: ${filename} (${(html.length / 1024).toFixed(0)}KB)`);

  return filepath;
}

/**
 * Get all clickable nav items in the sidebar
 */
async function getSidebarNavItems(page) {
  return await page.evaluate(() => {
    const items = [];

    // Find all links in the sidebar
    const sidebarLinks = document.querySelectorAll(
      'nav a, [role="navigation"] a, .MuiDrawer-root a, [class*="sidebar"] a'
    );

    sidebarLinks.forEach(link => {
      const text = link.textContent?.trim();
      const href = link.getAttribute('href');
      if (text && href && !href.startsWith('http') && !href.startsWith('mailto')) {
        items.push({ text, href, selector: null });
      }
    });

    // Also find any button-like nav items that aren't links
    const navButtons = document.querySelectorAll(
      'nav [role="button"], [role="navigation"] [role="button"], .MuiDrawer-root [role="treeitem"]'
    );

    navButtons.forEach(btn => {
      const text = btn.textContent?.trim();
      if (text && text.length < 50) {
        items.push({ text, href: null, isButton: true });
      }
    });

    return items;
  });
}

/**
 * Get all clickable elements on the current page (tabs, buttons, etc.)
 */
async function getPageInteractiveElements(page) {
  return await page.evaluate(() => {
    const items = [];

    // Tabs
    document.querySelectorAll('[role="tab"], [class*="tab" i]').forEach(el => {
      const text = el.textContent?.trim();
      if (text && text.length < 50) {
        items.push({ type: 'tab', text });
      }
    });

    // Expandable sections
    document.querySelectorAll('[aria-expanded], [class*="expand" i], [class*="accordion" i], [class*="collaps" i]').forEach(el => {
      const text = el.textContent?.trim().substring(0, 50);
      if (text) {
        items.push({ type: 'expandable', text });
      }
    });

    // Dropdown/select triggers
    document.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"], [class*="select" i][class*="trigger" i]').forEach(el => {
      const text = el.textContent?.trim();
      if (text && text.length < 50) {
        items.push({ type: 'dropdown', text });
      }
    });

    return items;
  });
}

/**
 * Crawl a single sidebar nav item
 */
async function crawlNavItem(page, item) {
  const itemKey = item.href || item.text;
  if (visitedUrls.has(itemKey)) {
    console.log(`  ⏭️  Skipping (already visited): ${item.text}`);
    return;
  }
  visitedUrls.add(itemKey);

  console.log(`\n📂 Navigating to: ${item.text} ${item.href ? `(${item.href})` : ''}`);

  try {
    if (item.href) {
      // Navigate via URL
      const fullUrl = item.href.startsWith('/') ? `${BASE_URL}${item.href}` : item.href;
      await page.goto(fullUrl, { waitUntil: 'networkidle', timeout: 15000 });
    } else {
      // Click the button/element
      const el = await page.locator(`text="${item.text}"`).first();
      if (await el.isVisible()) {
        await el.click();
        await page.waitForTimeout(WAIT_MS);
      }
    }

    // Save the main page DOM
    await saveDom(page, item.text);

    // Check for tabs or sub-navigation on this page
    await crawlPageTabs(page, item.text);

  } catch (err) {
    console.log(`  ⚠️  Error navigating to ${item.text}: ${err.message}`);
  }
}

/**
 * On the current page, find and click through any tabs or sub-views
 */
async function crawlPageTabs(page, parentName) {
  const interactiveElements = await getPageInteractiveElements(page);

  for (const el of interactiveElements) {
    const elKey = `${parentName}/${el.text}`;
    if (visitedButtons.has(elKey)) continue;
    visitedButtons.add(elKey);

    console.log(`    🔘 Found ${el.type}: "${el.text}"`);

    try {
      // Try to click the element
      const locator = page.locator(`text="${el.text}"`).first();
      if (await locator.isVisible({ timeout: 2000 })) {
        await locator.click();
        await page.waitForTimeout(2000);

        // Save the new state
        await saveDom(page, `${parentName}-${el.text}`);

        // If it opened a modal or drawer, close it
        const closeButton = page.locator('[aria-label="close"], [aria-label="Close"], button:has(svg[data-testid="CloseIcon"])').first();
        if (await closeButton.isVisible({ timeout: 1000 }).catch(() => false)) {
          await closeButton.click();
          await page.waitForTimeout(500);
        }
      }
    } catch (err) {
      // Silently continue - element might not be clickable
    }
  }
}

/**
 * Check if a project list exists and crawl into projects
 */
async function crawlProjects(page) {
  console.log('\n🔍 Looking for project list...');

  try {
    // Look for project links/cards on an "All Projects" type page
    const projectLinks = await page.evaluate(() => {
      const links = [];
      // Look for links that go to /project/...
      document.querySelectorAll('a[href*="/project/"]').forEach(a => {
        const text = a.textContent?.trim();
        const href = a.getAttribute('href');
        if (text && href && text.length < 100) {
          links.push({ text: text.substring(0, 50), href });
        }
      });
      return [...new Map(links.map(l => [l.href, l])).values()]; // dedupe by href
    });

    console.log(`  Found ${projectLinks.length} project links`);

    // Crawl each project (up to 5 to avoid going too deep)
    for (const project of projectLinks.slice(0, 5)) {
      if (visitedUrls.has(project.href)) continue;
      visitedUrls.add(project.href);

      console.log(`\n  📁 Entering project: ${project.text}`);
      const fullUrl = project.href.startsWith('/') ? `${BASE_URL}${project.href}` : project.href;

      try {
        await page.goto(fullUrl, { waitUntil: 'networkidle', timeout: 15000 });
        await saveDom(page, `project-${project.text}`);

        // Now crawl sidebar items inside this project
        const subNavItems = await getSidebarNavItems(page);
        for (const subItem of subNavItems) {
          await crawlNavItem(page, subItem);
        }
      } catch (err) {
        console.log(`    ⚠️  Error in project: ${err.message}`);
      }
    }
  } catch (err) {
    console.log(`  ⚠️  Error looking for projects: ${err.message}`);
  }
}

/**
 * Try clicking "Create new" buttons and capture the resulting modals/forms
 */
async function crawlCreateButtons(page) {
  console.log('\n🆕 Looking for "Create" / "New" buttons...');

  const createButtons = await page.evaluate(() => {
    const buttons = [];
    document.querySelectorAll('button, a').forEach(el => {
      const text = el.textContent?.trim().toLowerCase();
      if (text && (text.includes('create') || text.includes('new ') || text.includes('+ ') || text.includes('add '))) {
        buttons.push(el.textContent?.trim().substring(0, 50));
      }
    });
    return [...new Set(buttons)];
  });

  for (const btnText of createButtons) {
    const btnKey = `create-${btnText}`;
    if (visitedButtons.has(btnKey)) continue;
    visitedButtons.add(btnKey);

    console.log(`  🔘 Clicking: "${btnText}"`);

    try {
      const btn = page.locator(`text="${btnText}"`).first();
      if (await btn.isVisible({ timeout: 2000 })) {
        await btn.click();
        await page.waitForTimeout(2000);

        // Save whatever appeared (modal, new page, drawer, etc.)
        await saveDom(page, `create-${btnText}`);

        // Try to close any modal/dialog that opened
        const closeBtn = page.locator('[aria-label="close"], [aria-label="Close"], button:has(svg[data-testid="CloseIcon"]), [class*="close"]').first();
        if (await closeBtn.isVisible({ timeout: 1000 }).catch(() => false)) {
          await closeBtn.click();
          await page.waitForTimeout(500);
        } else {
          // If no close button, go back
          await page.goBack().catch(() => {});
          await page.waitForTimeout(1000);
        }
      }
    } catch (err) {
      // Continue silently
    }
  }
}

/**
 * Main crawler function
 */
async function main() {
  console.log('🚀 Juicebox DOM Crawler');
  console.log('========================\n');
  console.log(`Output directory: ${OUTPUT_DIR}\n`);

  // Check if we have a saved session from a previous login
  const hasSession = fs.existsSync(path.join(SESSION_DIR, 'cookies.json'));

  // Launch browser (non-headless so you can see what's happening)
  const browser = await chromium.launch({
    headless: false,
    args: ['--start-maximized'],
  });

  let context;

  if (hasSession) {
    // Reuse saved session — no login needed
    console.log('🔑 Found saved session. Reusing your login...\n');
    context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      storageState: path.join(SESSION_DIR, 'cookies.json'),
    });
  } else {
    // First run — need manual login
    context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
    });
  }

  const page = await context.newPage();

  // Navigate to Juicebox
  console.log('📱 Opening Juicebox...');
  await page.goto(START_URL, { waitUntil: 'networkidle', timeout: 30000 });

  // Check if we landed on a login page or the actual app
  const isLoginPage = await page.evaluate(() => {
    const text = document.body.innerText || '';
    return text.includes('Continue with Google') || text.includes('Get started for free');
  });

  if (isLoginPage) {
    console.log('   👉 You need to login with Google.');
    console.log('   👉 Login in the browser window, then press Enter here.\n');

    // Wait for user to login
    await new Promise(resolve => {
      process.stdin.once('data', resolve);
    });

    // Wait for the app to load after login
    await page.waitForTimeout(5000);

    // Save the session so next time we skip login
    if (!fs.existsSync(SESSION_DIR)) {
      fs.mkdirSync(SESSION_DIR, { recursive: true });
    }
    await context.storageState({ path: path.join(SESSION_DIR, 'cookies.json') });
    console.log('💾 Session saved! Next run will skip login.\n');
  } else {
    console.log('✅ Already logged in (session reused).\n');
  }

  console.log('🚀 Starting crawl...\n');

  // Also take a screenshot for reference
  await page.screenshot({
    path: path.join(OUTPUT_DIR, '000-screenshot-start.png'),
    fullPage: true
  });

  // Step 2: Capture the initial page (Agent page)
  await saveDom(page, 'agent-home');

  // Step 3: Get all sidebar nav items
  console.log('\n📋 Scanning sidebar navigation...');
  const navItems = await getSidebarNavItems(page);
  console.log(`   Found ${navItems.length} nav items:`);
  navItems.forEach(item => console.log(`   - ${item.text} ${item.href || '(button)'}`));

  // Step 4: Crawl each nav item
  for (const item of navItems) {
    await crawlNavItem(page, item);
  }

  // Step 5: Go to "All Projects" and crawl project list
  try {
    const allProjectsLink = navItems.find(i =>
      i.text.toLowerCase().includes('all projects') ||
      i.text.toLowerCase().includes('projects')
    );
    if (allProjectsLink?.href) {
      await page.goto(`${BASE_URL}${allProjectsLink.href}`, { waitUntil: 'networkidle', timeout: 15000 });
      await saveDom(page, 'all-projects-list');
      await crawlProjects(page);
    }
  } catch (err) {
    console.log(`  ⚠️  Error crawling projects: ${err.message}`);
  }

  // Step 6: Look for "Create new" buttons and capture their modals
  // Go back through key pages and find create buttons
  for (const item of navItems) {
    if (item.href) {
      try {
        const fullUrl = `${BASE_URL}${item.href}`;
        await page.goto(fullUrl, { waitUntil: 'networkidle', timeout: 10000 });
        await crawlCreateButtons(page);
      } catch (err) {
        // Continue
      }
    }
  }

  // Step 7: Capture any settings/account pages
  console.log('\n⚙️  Crawling settings...');
  const settingsUrls = [
    '/account?tab=user',
    '/account?tab=team',
    '/account?tab=billing',
  ];

  for (const settingsUrl of settingsUrls) {
    const key = settingsUrl;
    if (visitedUrls.has(key)) continue;
    visitedUrls.add(key);

    try {
      await page.goto(`${BASE_URL}${settingsUrl}`, { waitUntil: 'networkidle', timeout: 10000 });
      const tabName = settingsUrl.split('tab=')[1] || 'settings';
      await saveDom(page, `settings-${tabName}`);
    } catch (err) {
      console.log(`  ⚠️  Error on settings: ${err.message}`);
    }
  }

  // Done!
  console.log('\n\n🎉 Crawl complete!');
  console.log(`📁 ${captureCount} HTML files saved to: ${OUTPUT_DIR}`);
  console.log('\nFiles captured:');

  const files = fs.readdirSync(OUTPUT_DIR).filter(f => f.endsWith('.html')).sort();
  files.forEach(f => {
    const size = (fs.statSync(path.join(OUTPUT_DIR, f)).size / 1024).toFixed(0);
    console.log(`  ${f} (${size}KB)`);
  });

  // Generate an index file
  const index = files.map(f => {
    const content = fs.readFileSync(path.join(OUTPUT_DIR, f), 'utf-8');
    const urlMatch = content.match(/URL: (.+)/);
    const nameMatch = content.match(/Name: (.+)/);
    return `- [${nameMatch?.[1] || f}](${f}) — ${urlMatch?.[1] || 'N/A'}`;
  }).join('\n');

  fs.writeFileSync(path.join(OUTPUT_DIR, 'INDEX.md'), `# Juicebox HTML Captures\n\n${index}\n`);
  console.log('\n📄 INDEX.md created');

  await browser.close();
}

main().catch(err => {
  console.error('❌ Crawler error:', err);
  process.exit(1);
});
