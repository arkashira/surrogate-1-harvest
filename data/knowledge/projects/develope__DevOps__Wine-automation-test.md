---
name: Wine-automation-test
path: /Users/Ashira/develope/DevOps/Wine-automation-test
tags: ["project", "codebase", "javascript-typescript", "typescript", "aws-sdk"]
last_indexed: 2026-05-01
type: project
---

# Wine-automation-test

**Path**: `/Users/Ashira/develope/DevOps/Wine-automation-test`
**Group**: DevOps
**Languages**: JavaScript/TypeScript
**Frameworks**: TypeScript, AWS SDK
**LOC**: ~204,194
**Deps**: 16

## README
End-to-end + API automation test suite for the Excise Wine product line. Env-switchable between **staging** and **prod** via a single flag. | Service         | Stack              | Staging URL                                           | Prod URL                                       | Tool           | |-----------------|--------------------|-------------------------------------------------------|------------------------------------------------|----------------|

## Git
- Branch: `?`
- Last commit: ?
- Commits (last 30d): ?

## Key dependencies
- `@aws-sdk/client-s3`
- `@faker-js/faker`
- `@playwright/test`
- `@types/jest`
- `@types/node`
- `axios`
- `cross-env`
- `dotenv`
- `form-data`
- `jest`
- `jest-junit`
- `newman`
- `ts-jest`
- `tsx`
- `typescript`

## Scripts
- `test`
- `test:api`
- `test:api:node`
- `test:api:go`
- `test:e2e`
- `test:e2e:pricesearch`
- `test:e2e:fasttrack`
- `test:smoke`
- `test:staging`
- `test:prod`

## Structure
```
📄 KNOWN_ISSUES.md
📄 README.md
📁 config
  📄 env.ts
  📄 evidence-reporter.ts
  📄 evidence.ts
  📄 http.ts
  📄 jest.global-setup.js
  📄 jest.setup.ts
📁 fixtures
  📄 auth.ts
  📁 images
    📄 wine-sample-1.jpg
    📄 wine-sample-2.jpg
    📄 wine-sample-3.jpg
    📄 wine-sample-4.jpg
  📄 s3-wine-images.ts
  📄 users.ts
  📄 wine-data.ts
📄 jest.config.api.js
📄 package-lock.json
📄 package.json
📄 playwright.config.ts
📁 postman
  📄 ExciseWine-Production.postman_environment.json
  📄 ExciseWine-Staging.postman_environment.json
  📄 ExciseWine-collection.json
  📄 README.md
📁 reports
  📁 evidence
    📁 api
    📁 mobile
    📁 playwright
  📁 excel
    📄 Automation-test-report.zip
    📄 IVT-PVT-prod-v2.0.0.xlsx
    📄 QA-Dev-Checklist-v2.0.0.xlsx
    📄 S3-Migration-filled.xlsx
    📄 automation-test-report.xlsx
    📄 release-checklist-filled.xlsx
    📄 ~$release-checklist-filled.xlsx
  📄 jest-junit.xml
  📁 playwright-artifacts
    📁 auth-Fasttrack-auth-—-bad--319f5--password-SPA-route-renders-fasttrack
    📁 auth-Fasttrack-auth-—-bad--c7f77-tials-shows-error-no-crash--fasttrack
    📁 auth-Fasttrack-auth-—-bad--db3aa--password-SPA-route-renders-fasttrack
    📁 auth-Fasttrack-auth-—-bad-paths-register-SPA-route-renders-fasttrack
    📁 auth-Fasttrack-auth-—-good-93a90-er-credentials-reaches-home-fasttrack
    📁 auth-PriceSearch-auth-page-22326-er-existence-via-error-text-pricesearch
    📁 auth-PriceSearch-auth-page-25f21--password-SPA-route-renders-pricesearch
    📁 auth-PriceSearch-auth-page-4d69e--password-SPA-route-renders-pricesearch
    📁 authenticated-flow-Fasttra-4aea6-session-clears-on-fresh-tab-fasttrack
    📁 authenticated-flow-Fasttra-6ba02-erage-navigate-to-wine-list-fasttrack
    📁 authenticated-flow-Fasttra-7b693-ate-to-check-wine-by-upload-fasttrack
    📁 authenticated-flow-Fasttra-9014f-avigate-to-import-wine-page-fasttrack
    📁 authenticated-flow-Fasttra-e6ab8-rd-home-after-login-renders-fasttrack
    📁 cart-Fasttrack-cart-routes-387b9-oads-gate-OR-loading-state--fasttrack
    📁 cart-Fasttrack-cart-routes-4bae9-d-format-does-not-crash-app-fasttrack
    📁 cart-Fasttrack-cart-routes-95572-s-gate-handled-client-side--fasttrack
    📁 line-detail-PriceSearch-Li-257f9--missing-id-param-no-crash--pricesearch
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
