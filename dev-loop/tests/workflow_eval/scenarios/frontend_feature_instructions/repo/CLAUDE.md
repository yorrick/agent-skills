# Development Guidelines

## Testing
- Run e2e tests with `npx playwright test` after any UI change
- Use Playwright for all end-to-end tests
- Component tests go in tests/e2e/

## Quality Gates
- Run `npm run build` to verify no build errors
- Always test on both light and dark mode
