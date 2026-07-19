import eslint from '@eslint/js'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default [
  {
    ignores: ['../static/app/**'],
  },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.ts'],
    languageOptions: {
      globals: globals.browser,
    },
  },
  {
    files: ['e2e/**/*.ts'],
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
  },
  {
    files: ['playwright.config.ts'],
    languageOptions: {
      globals: globals.node,
    },
  },
]
