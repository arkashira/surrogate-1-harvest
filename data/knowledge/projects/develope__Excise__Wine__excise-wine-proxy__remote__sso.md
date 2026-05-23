---
name: sso
path: /Users/Ashira/develope/Excise/Wine/excise-wine-proxy/remote/sso
tags: ["project", "codebase", "javascript-typescript", "nestjs", "typescript", "docker"]
last_indexed: 2026-05-01
type: project
---

# sso

**Path**: `/Users/Ashira/develope/Excise/Wine/excise-wine-proxy/remote/sso`
**Group**: remote
**Languages**: JavaScript/TypeScript
**Frameworks**: NestJS, TypeScript, Docker
**LOC**: ~594
**Deps**: 35

## README
<p align="center"> <a href="http://nestjs.com/" target="blank"><img src="https://nestjs.com/img/logo-small.svg" width="120" alt="Nest Logo" /></a> </p> [circleci-image]: https://img.shields.io/circleci/build/github/nestjs/nest/master?token=abc123def456 [circleci-url]: https://circleci.com/gh/nestjs/nest <p align="center">A progressive <a href="http://nodejs.org" target="_blank">Node.js</a> framework for building efficient and scalable server-side applications.</p>

## Git
- Branch: `main`
- Last commit: 2026-04-22 16:30:36 +0700 refactor: remove trailing slashes from nginx location blocks and proxy_pass paths
- Commits (last 30d): 13

## Key dependencies
- `@eslint/eslintrc`
- `@eslint/js`
- `@nestjs/cli`
- `@nestjs/common`
- `@nestjs/core`
- `@nestjs/platform-express`
- `@nestjs/schematics`
- `@nestjs/terminus`
- `@nestjs/testing`
- `@types/express`
- `@types/jest`
- `@types/node`
- `@types/supertest`
- `@typescript-eslint/eslint-plugin`
- `@typescript-eslint/parser`

## Scripts
- `build`
- `build:webpack`
- `format`
- `start`
- `start:dev`
- `start:debug`
- `start:prod`
- `lint`
- `test`
- `test:watch`

## Structure
```
📄 Dockerfile
📄 README.md
📄 eslint.config.mjs
📄 nest-cli.json
📄 package-lock.json
📄 package.json
📁 src
  📄 app.controller.ts
  📄 app.module.ts
  📄 app.service.ts
  📁 common
    📁 dto
    📁 services
  📁 health
    📄 health.controller.ts
    📄 health.module.ts
    📄 health.service.ts
  📁 landing
    📁 dto
    📄 landing.controller.ts
    📄 landing.module.ts
    📄 landing.service.ts
  📄 main.ts
  📁 pipeList
    📄 allowed-values.pipe.ts
  📁 sso
    📁 dto
    📄 sso.controller.ts
    📄 sso.module.ts
    📄 sso.service.ts
📁 test
  📄 app.e2e-spec.ts
  📄 jest-e2e.json
📄 tsconfig.build.json
📄 tsconfig.json
📄 webpack.config.js
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
