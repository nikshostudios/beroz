/**
 * Juicebox DOM Crawler — Round 2 (Missing Pages)
 *
 * Captures the pages the first crawl missed:
 * - Search page + search results
 * - Candidate detail views
 * - Agent chat conversation
 * - Sequence details
 * - Analytics sub-tabs
 * - Contact details
 *
 * Usage:
 *   node crawl-missing.js
 */

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE_URL = 'https://app.juicebox.ai';
const PROJECT_ID = 'azOIT7xZWnbDRh7RHxuA';
const OUTPUT_DIR = path.join(__dirname, 'html-captures');
const SESSION_DIR = path.join(__dirname, 'auth-session');
const WAIT_MS = 4000;

let captureCount = 43; // Continue numbering from first crawl

if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}

function toFilename(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').substring(0, 60);
}

async function saveDom(page, name) {
  captureCount++;
  const filename = `${String(captureCount).padStart(3, '0')}-${toFilename(name)}.html`;
  const filepath = path.join(OUTPUT_DIR, filename);

  await page.waitForTimeout(WAIT_MS);

  const html = await page.evaluate(() => document.documentElement.outerHTML);
  const url = page.url();
  const title = await page.title();

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

async function takeScreenshot(page, name) {
  const filename = `screenshot-${toFilename(name)}.png`;
  await page.screenshot({
    path: path.join(OUTPUT_DIR, filename),
    fullPage: true
  });
  console.log(`  📸 Screenshot: ${filename}`);
}

async function main() {
  console.log('🚀 Juicebox DOM Crawler — Round 2 (Missing Pages)');
  console.log('===================================================\n');

  const hasSession = fs.existsSync(path.join(SESSION_DIR, 'cookies.json'));

  const browser = await chromium.launch({
    headless: false,
    args: ['--start-maximized'],
  });

  let context;
  if (hasSession) {
    console.log('🔑 Reusing saved session...\n');
    context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      storageState: path.join(SESSION_DIR, 'cookies.json'),
    });
  } else {
    context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
    });
  }

  const page = await context.newPage();

  // Navigate and check login
  await page.goto(`${BASE_URL}/project/${PROJECT_ID}/agent`, {
    waitUntil: 'networkidle',
    timeout: 30000
  });

  const isLoginPage = await page.evaluate(() => {
    const text = document.body.innerText || '';
    return text.includes('Continue with Google') || text.includes('Get started for free');
  });

  if (isLoginPage) {
    console.log('👉 Login required. Log in with Google, then press Enter.\n');
    await new Promise(resolve => { process.stdin.once('data', resolve); });
    await page.waitForTimeout(5000);
    if (!fs.existsSync(SESSION_DIR)) fs.mkdirSync(SESSION_DIR, { recursive: true });
    await context.storageState({ path: path.join(SESSION_DIR, 'cookies.json') });
    console.log('💾 Session saved.\n');
  }

  console.log('Starting captures...\n');

  // ============================================
  // 1. SEARCH PAGE — The most important one
  // ============================================
  console.log('━━━ 1. SEARCH PAGE ━━━');

  // First, navigate to the search page
  try {
    // Try navigating to search directly
    await page.goto(`${BASE_URL}/project/${PROJECT_ID}/search`, {
      waitUntil: 'networkidle',
      timeout: 15000
    });
    await saveDom(page, 'search-page-empty');
    await takeScreenshot(page, 'search-page-empty');

    // Look for any existing searches in the sidebar
    const searchLinks = await page.evaluate(() => {
      const links = [];
      document.querySelectorAll('a[href*="/search"]').forEach(a => {
        const text = a.textContent?.trim();
        const href = a.getAttribute('href');
        if (text && href && href.includes('search_id')) {
          links.push({ text: text.substring(0, 60), href });
        }
      });
      return links;
    });

    console.log(`  Found ${searchLinks.length} existing searches`);

    // Click into the first existing search if available
    for (const search of searchLinks.slice(0, 3)) {
      console.log(`\n  📂 Opening search: ${search.text}`);
      const fullUrl = search.href.startsWith('/') ? `${BASE_URL}${search.href}` : search.href;
      await page.goto(fullUrl, { waitUntil: 'networkidle', timeout: 15000 });
      await saveDom(page, `search-results-${search.text}`);
      await takeScreenshot(page, `search-results-${search.text}`);

      // Check for Results / Insights tabs
      try {
        const resultsTab = page.locator('text="Results"').first();
        if (await resultsTab.isVisible({ timeout: 2000 })) {
          await resultsTab.click();
          await page.waitForTimeout(2000);
          await saveDom(page, `search-results-tab-${search.text}`);
        }
      } catch (e) {}

      try {
        const insightsTab = page.locator('text="Insights"').first();
        if (await insightsTab.isVisible({ timeout: 2000 })) {
          await insightsTab.click();
          await page.waitForTimeout(2000);
          await saveDom(page, `search-insights-tab-${search.text}`);
        }
      } catch (e) {}

      // Click on the first candidate to get candidate detail view
      try {
        // Look for candidate name links in search results
        const candidateLinks = await page.evaluate(() => {
          const links = [];
          // Candidate names are usually prominent text elements in result cards
          document.querySelectorAll('[class*="result"] a, [class*="candidate"] a, [data-testid*="candidate"]').forEach(el => {
            const text = el.textContent?.trim();
            if (text && text.length > 3 && text.length < 60) {
              links.push(text);
            }
          });
          // Also try looking for any clickable candidate cards/rows
          document.querySelectorAll('[role="row"] td:first-child, [class*="card"] [class*="name"]').forEach(el => {
            const text = el.textContent?.trim();
            if (text && text.length > 3 && text.length < 60) {
              links.push(text);
            }
          });
          return [...new Set(links)].slice(0, 3);
        });

        if (candidateLinks.length > 0) {
          console.log(`  Found ${candidateLinks.length} candidates to click`);

          // Click the first candidate
          const firstCandidate = page.locator(`text="${candidateLinks[0]}"`).first();
          if (await firstCandidate.isVisible({ timeout: 2000 })) {
            await firstCandidate.click();
            await page.waitForTimeout(3000);
            await saveDom(page, `candidate-detail-${candidateLinks[0]}`);
            await takeScreenshot(page, `candidate-detail-${candidateLinks[0]}`);

            // Try to close the detail panel
            const closeBtn = page.locator('[aria-label="close"], [aria-label="Close"], button:has(svg[data-testid="CloseIcon"])').first();
            if (await closeBtn.isVisible({ timeout: 1000 }).catch(() => false)) {
              await closeBtn.click();
              await page.waitForTimeout(500);
            }
          }
        }
      } catch (e) {
        console.log(`  ⚠️ Could not click candidate: ${e.message}`);
      }

      // Click on a status dropdown (Shortlisted, Contacted, etc.)
      try {
        const statusDropdown = page.locator('[class*="status"] select, button:has-text("Shortlisted"), button:has-text("Contacted"), [aria-haspopup]:has-text("Shortlisted")').first();
        if (await statusDropdown.isVisible({ timeout: 2000 })) {
          await statusDropdown.click();
          await page.waitForTimeout(1500);
          await saveDom(page, `search-status-dropdown-open`);
          // Click away to close
          await page.click('body', { position: { x: 10, y: 10 } });
          await page.waitForTimeout(500);
        }
      } catch (e) {}

      // Click Filters button
      try {
        const filtersBtn = page.locator('button:has-text("Filters"), [aria-label*="filter" i]').first();
        if (await filtersBtn.isVisible({ timeout: 2000 })) {
          await filtersBtn.click();
          await page.waitForTimeout(2000);
          await saveDom(page, `search-filters-panel`);
          await takeScreenshot(page, `search-filters-panel`);
          // Close it
          await page.keyboard.press('Escape');
          await page.waitForTimeout(500);
        }
      } catch (e) {}

      // Click Criteria button
      try {
        const criteriaBtn = page.locator('button:has-text("Criteria")').first();
        if (await criteriaBtn.isVisible({ timeout: 2000 })) {
          await criteriaBtn.click();
          await page.waitForTimeout(2000);
          await saveDom(page, `search-criteria-panel`);
          await takeScreenshot(page, `search-criteria-panel`);
          await page.keyboard.press('Escape');
          await page.waitForTimeout(500);
        }
      } catch (e) {}

      // Click "+ New Search" button if visible
      try {
        const newSearchBtn = page.locator('button:has-text("New Search")').first();
        if (await newSearchBtn.isVisible({ timeout: 2000 })) {
          await newSearchBtn.click();
          await page.waitForTimeout(2000);
          await saveDom(page, `new-search-modal`);
          await takeScreenshot(page, `new-search-modal`);
          await page.keyboard.press('Escape');
          await page.waitForTimeout(500);
        }
      } catch (e) {}

      // Try different view toggles (list view, card view, review)
      try {
        const viewToggles = page.locator('[aria-label*="view" i], button:has-text("Review")');
        const count = await viewToggles.count();
        for (let i = 0; i < Math.min(count, 3); i++) {
          const toggle = viewToggles.nth(i);
          if (await toggle.isVisible({ timeout: 1000 })) {
            const label = await toggle.getAttribute('aria-label') || await toggle.textContent();
            await toggle.click();
            await page.waitForTimeout(2000);
            await saveDom(page, `search-view-${label?.trim()}`);
          }
        }
      } catch (e) {}

      break; // Only need to deep-crawl one search
    }

    // If no existing searches, try the "Searches" sidebar link
    if (searchLinks.length === 0) {
      try {
        const searchesLink = page.locator('text="Searches"').first();
        if (await searchesLink.isVisible({ timeout: 2000 })) {
          await searchesLink.click();
          await page.waitForTimeout(3000);
          await saveDom(page, 'searches-sidebar');
        }
      } catch (e) {}
    }

  } catch (err) {
    console.log(`  ⚠️ Search page error: ${err.message}`);
  }

  // ============================================
  // 2. AGENT CHAT — Actual conversation
  // ============================================
  console.log('\n━━━ 2. AGENT CHAT CONVERSATION ━━━');

  try {
    await page.goto(`${BASE_URL}/project/${PROJECT_ID}/agent`, {
      waitUntil: 'networkidle',
      timeout: 15000
    });

    // Click "Yes, start sourcing" or similar CTA if present
    const startBtn = page.locator('button:has-text("start sourcing"), button:has-text("Start"), [class*="cta"]').first();
    if (await startBtn.isVisible({ timeout: 3000 })) {
      await startBtn.click();
      await page.waitForTimeout(4000);
      await saveDom(page, 'agent-after-start-sourcing');
      await takeScreenshot(page, 'agent-after-start-sourcing');
    }

    // Try typing in the agent chat input
    const chatInput = page.locator('textarea, input[type="text"], [contenteditable="true"], [class*="input"][class*="chat"]').first();
    if (await chatInput.isVisible({ timeout: 3000 })) {
      await chatInput.click();
      await page.waitForTimeout(500);
      await saveDom(page, 'agent-chat-input-focused');
    }

  } catch (err) {
    console.log(`  ⚠️ Agent chat error: ${err.message}`);
  }

  // ============================================
  // 3. ANALYTICS SUB-TABS
  // ============================================
  console.log('\n━━━ 3. ANALYTICS SUB-TABS ━━━');

  const analyticsTabs = ['usage', 'projects', 'outreach', 'agents'];
  for (const tab of analyticsTabs) {
    try {
      await page.goto(`${BASE_URL}/project/${PROJECT_ID}/analytics/${tab}`, {
        waitUntil: 'networkidle',
        timeout: 10000
      });
      await saveDom(page, `analytics-${tab}`);
    } catch (err) {
      console.log(`  ⚠️ Analytics ${tab}: ${err.message}`);
    }
  }

  // Also try clicking sub-nav items within analytics
  try {
    await page.goto(`${BASE_URL}/project/${PROJECT_ID}/analytics/usage`, {
      waitUntil: 'networkidle',
      timeout: 10000
    });

    // Look for sub-tabs like Usage, Projects, Outreach, Agents
    const subTabs = page.locator('[role="tab"], a[href*="analytics"]');
    const tabCount = await subTabs.count();
    for (let i = 0; i < tabCount; i++) {
      const tab = subTabs.nth(i);
      const text = await tab.textContent();
      if (text && await tab.isVisible({ timeout: 1000 })) {
        await tab.click();
        await page.waitForTimeout(2000);
        await saveDom(page, `analytics-tab-${text.trim()}`);
      }
    }
  } catch (e) {}

  // ============================================
  // 4. SEQUENCE DETAIL VIEW
  // ============================================
  console.log('\n━━━ 4. SEQUENCE DETAIL ━━━');

  try {
    await page.goto(`${BASE_URL}/project/${PROJECT_ID}/sequences`, {
      waitUntil: 'networkidle',
      timeout: 10000
    });

    // Click on the first sequence to see its detail
    const sequenceLinks = await page.evaluate(() => {
      const links = [];
      document.querySelectorAll('a[href*="sequence"], [class*="sequence"] [role="button"], tr[class*="row"], [class*="card"]').forEach(el => {
        const text = el.textContent?.trim().substring(0, 50);
        if (text && text.length > 3) links.push(text);
      });
      return [...new Set(links)].slice(0, 3);
    });

    for (const seq of sequenceLinks.slice(0, 2)) {
      try {
        const seqEl = page.locator(`text="${seq}"`).first();
        if (await seqEl.isVisible({ timeout: 2000 })) {
          await seqEl.click();
          await page.waitForTimeout(3000);
          await saveDom(page, `sequence-detail-${seq}`);
          await takeScreenshot(page, `sequence-detail-${seq}`);

          // Go back to sequence list
          await page.goBack();
          await page.waitForTimeout(2000);
        }
      } catch (e) {}
    }
  } catch (err) {
    console.log(`  ⚠️ Sequence detail error: ${err.message}`);
  }

  // ============================================
  // 5. CONTACT DETAIL VIEW
  // ============================================
  console.log('\n━━━ 5. CONTACT DETAIL ━━━');

  try {
    await page.goto(`${BASE_URL}/project/${PROJECT_ID}/contacts`, {
      waitUntil: 'networkidle',
      timeout: 10000
    });

    // Click on the first contact
    const contactRows = page.locator('table tbody tr, [class*="contact"][class*="row"], [class*="list"] [class*="item"]');
    const contactCount = await contactRows.count();

    if (contactCount > 0) {
      await contactRows.first().click();
      await page.waitForTimeout(3000);
      await saveDom(page, 'contact-detail-view');
      await takeScreenshot(page, 'contact-detail-view');
    }
  } catch (err) {
    console.log(`  ⚠️ Contact detail error: ${err.message}`);
  }

  // ============================================
  // 6. SHORTLIST — Candidate interaction
  // ============================================
  console.log('\n━━━ 6. SHORTLIST DETAIL ━━━');

  try {
    await page.goto(`${BASE_URL}/project/${PROJECT_ID}/shortlist`, {
      waitUntil: 'networkidle',
      timeout: 10000
    });

    // Click first candidate in shortlist
    const shortlistRows = page.locator('table tbody tr, [class*="candidate"], [class*="card"]');
    const slCount = await shortlistRows.count();

    if (slCount > 0) {
      await shortlistRows.first().click();
      await page.waitForTimeout(3000);
      await saveDom(page, 'shortlist-candidate-detail');
      await takeScreenshot(page, 'shortlist-candidate-detail');
    }
  } catch (err) {
    console.log(`  ⚠️ Shortlist detail error: ${err.message}`);
  }

  // ============================================
  // 7. PROJECT SELECTOR DROPDOWN
  // ============================================
  console.log('\n━━━ 7. PROJECT SELECTOR ━━━');

  try {
    await page.goto(`${BASE_URL}/project/${PROJECT_ID}/agent`, {
      waitUntil: 'networkidle',
      timeout: 10000
    });

    // Click the project selector dropdown in sidebar
    const projectSelector = page.locator('[role="combobox"], [aria-haspopup="listbox"], [class*="project"][class*="select"]').first();
    if (await projectSelector.isVisible({ timeout: 3000 })) {
      await projectSelector.click();
      await page.waitForTimeout(2000);
      await saveDom(page, 'project-selector-dropdown-open');
      await takeScreenshot(page, 'project-selector-dropdown-open');
      await page.keyboard.press('Escape');
    }
  } catch (err) {
    console.log(`  ⚠️ Project selector error: ${err.message}`);
  }

  // ============================================
  // 8. SHARE / EXPORT MODALS
  // ============================================
  console.log('\n━━━ 8. SHARE & EXPORT MODALS ━━━');

  try {
    // Go to search results and click Share
    await page.goto(`${BASE_URL}/project/${PROJECT_ID}/search`, {
      waitUntil: 'networkidle',
      timeout: 10000
    });

    const shareBtn = page.locator('button:has-text("Share")').first();
    if (await shareBtn.isVisible({ timeout: 3000 })) {
      await shareBtn.click();
      await page.waitForTimeout(2000);
      await saveDom(page, 'share-modal');
      await takeScreenshot(page, 'share-modal');
      await page.keyboard.press('Escape');
    }
  } catch (e) {}

  // ============================================
  // 9. USER MENU / PROFILE DROPDOWN
  // ============================================
  console.log('\n━━━ 9. USER MENU ━━━');

  try {
    // Click user avatar/name in sidebar
    const userProfile = page.locator('[class*="avatar"], img[src*="googleusercontent"]').first();
    if (await userProfile.isVisible({ timeout: 3000 })) {
      await userProfile.click();
      await page.waitForTimeout(2000);
      await saveDom(page, 'user-menu-open');
    }
  } catch (e) {}

  // ============================================
  // DONE
  // ============================================
  const newCaptures = captureCount - 43;
  console.log(`\n\n🎉 Round 2 complete! ${newCaptures} additional HTML files captured.`);
  console.log(`📁 Total: ${captureCount} files in ${OUTPUT_DIR}\n`);

  // List new files
  const files = fs.readdirSync(OUTPUT_DIR)
    .filter(f => f.endsWith('.html'))
    .sort()
    .filter(f => parseInt(f.substring(0, 3)) > 43);

  files.forEach(f => {
    const size = (fs.statSync(path.join(OUTPUT_DIR, f)).size / 1024).toFixed(0);
    console.log(`  ${f} (${size}KB)`);
  });

  // Update INDEX.md
  const allFiles = fs.readdirSync(OUTPUT_DIR).filter(f => f.endsWith('.html')).sort();
  const index = allFiles.map(f => {
    const content = fs.readFileSync(path.join(OUTPUT_DIR, f), 'utf-8');
    const urlMatch = content.match(/URL: (.+)/);
    const nameMatch = content.match(/Name: (.+)/);
    return `- [${nameMatch?.[1] || f}](${f}) — ${urlMatch?.[1] || 'N/A'}`;
  }).join('\n');

  fs.writeFileSync(path.join(OUTPUT_DIR, 'INDEX.md'), `# Juicebox HTML Captures\n\nTotal: ${allFiles.length} files\n\n${index}\n`);
  console.log('\n📄 INDEX.md updated');

  await browser.close();
}

main().catch(err => {
  console.error('❌ Error:', err);
  process.exit(1);
});
