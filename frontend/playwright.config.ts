import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e', fullyParallel: false,
  use: { screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  projects: [
    { name: 'local', testIgnore: '**/*-auth.spec.ts', use: { baseURL: 'http://127.0.0.1:4175' } },
    { name: 'authenticated', testMatch: '**/*-auth.spec.ts', use: { baseURL: 'http://127.0.0.1:4176' } },
  ],
  webServer: [
    {
      command: 'VITE_AUTH_ENABLED=false VITE_INLUMEN_API_URL=http://127.0.0.1:4175 npm run dev -- --host 127.0.0.1 --port 4175 --strictPort',
      url: 'http://127.0.0.1:4175', reuseExistingServer: false,
    },
    {
      command: 'VITE_AUTH_ENABLED=true VITE_TOOLBOX_ORIGIN=http://127.0.0.1:4176 VITE_INLUMEN_API_URL=http://127.0.0.1:4176 npm run dev -- --host 127.0.0.1 --port 4176 --strictPort',
      url: 'http://127.0.0.1:4176', reuseExistingServer: false,
    },
  ],
});
