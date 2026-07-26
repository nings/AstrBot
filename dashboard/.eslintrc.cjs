/* eslint-env node */
require('@rushstack/eslint-patch/modern-module-resolution')

module.exports = {
  root: true,
  extends: [
    'plugin:vue/vue3-essential',
    'eslint:recommended',
    '@vue/eslint-config-typescript',
    '@vue/eslint-config-prettier/skip-formatting'
  ],
  parserOptions: {
    ecmaVersion: 'latest'
  }
  // The end-user portal SPA's capability wall used to live here as a
  // no-restricted-imports override, back when that SPA was a second Vite entry
  // in this workspace and had to be kept away from the admin-only chat surface.
  // It is now a separate project (AstrBotDevs/astrbot-portal) and the boundary
  // is structural, so the rule moved to that repository's own eslint config.
  // Only the operator dashboard is built here.
}
