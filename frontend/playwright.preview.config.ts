import { defineConfig } from '@playwright/test'
import ui from './playwright.ui.config'

// Requires the synthetic Word/PDF output of scripts.verify_visual_review.
// Reuse the isolated local server; never point this suite at a live deployment.
export default defineConfig({
  ...ui,
  testMatch: ['visual-review.spec.ts'],
  timeout: 60000,
})
