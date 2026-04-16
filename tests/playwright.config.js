// @ts-check
const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './',
  timeout: 60000,
  retries: 1,
  use: {
    baseURL: 'https://exceltechcomputers.up.railway.app',
    headless: true,
    screenshot: 'only-on-failure',
    video: 'off',
    navigationTimeout: 30000,
    actionTimeout: 15000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
