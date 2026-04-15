# Juicebox DOM Crawler

## Setup (one time)

```bash
cd ~/Claude\ Workspace/exceltech-ai/recruitment-agents/juicebox-crawler
npm install
npx playwright install chromium
```

## Run

```bash
npm run crawl
```

## What happens

1. Chrome opens and goes to Juicebox
2. **You log in with Google manually**
3. Once you see the dashboard, **press Enter in the terminal**
4. The crawler takes over — clicking every nav item, tab, button, and sub-page
5. Each page's full live DOM is saved as an HTML file in `html-captures/`

## Output

```
html-captures/
  001-agent-home.html
  002-all-projects.html
  003-all-agents.html
  004-agent.html
  005-shortlist.html
  006-contacts.html
  007-sequences.html
  008-analytics.html
  ...
  INDEX.md              ← list of all captures with URLs
```
