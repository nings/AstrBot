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
  },
  overrides: [
    {
      // portal capability wall (see design/portal-multiuser-plan.md
      // section 4 / pyproject.toml's [tool.importlinter] for the backend half of
      // this same guarantee): the end-user portal SPA must never pull in the
      // admin-only chat surface -- these components/composables assume an
      // authenticated *operator* (provider/model pickers, raw command
      // injection, multi-tenant session admin) and must not end up in the
      // end-user bundle's dependency graph, even transitively.
      files: ['src/components/portal/**/*.{vue,ts,js}', 'src/portal-main.ts'],
      rules: {
        'no-restricted-imports': [
          'error',
          {
            patterns: [
              {
                group: [
                  '**/ConfigSelector.vue',
                  '**/ProviderModelMenu.vue',
                  '**/CommandSuggestion.vue',
                  '**/Chat.vue',
                  '**/ChatMessageList.vue',
                  '**/useSessions.ts',
                  '**/useSessions'
                ],
                message:
                  'The end-user portal module must not import the admin dashboard chat surface ' +
                  '(ConfigSelector.vue / ProviderModelMenu.vue / CommandSuggestion.vue / Chat.vue / ' +
                  'ChatMessageList.vue / useSessions.ts). Build/reuse a portal-scoped equivalent instead.'
              }
            ]
          }
        ]
      }
    }
  ]
}
