import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e', fullyParallel: false,
  use: { baseURL: 'http://127.0.0.1:4175', screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  webServer: {
    command: 'VITE_AUTH_ENABLED=false VITE_INLUMEN_API_URL=http://127.0.0.1:4175 npm run dev -- --host 127.0.0.1 --port 4175',
    url: 'http://127.0.0.1:4175', reuseExistingServer: false,
  },
});
