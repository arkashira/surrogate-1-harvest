---
name: excise-wine-nodejs-api
path: /Users/Ashira/develope/Excise/Wine/excise-wine-nodejs-api
tags: ["project", "codebase", "javascript-typescript", "express", "typescript", "firebase", "aws-sdk", "sequelize"]
last_indexed: 2026-05-01
type: project
---

# excise-wine-nodejs-api

**Path**: `/Users/Ashira/develope/Excise/Wine/excise-wine-nodejs-api`
**Group**: Wine
**Languages**: JavaScript/TypeScript
**Frameworks**: Express, TypeScript, Firebase, AWS SDK, Sequelize, MSSQL, Docker
**LOC**: ~64,794
**Deps**: 52

## README
excise wine nodejs-api - CI/CD managed repository

## Git
- Branch: `staging/aws`
- Last commit: 2026-04-30 11:23:44 +0700 requie only wineName for check verify
- Commits (last 30d): 100

## Key dependencies
- `@aws-sdk/client-cloudwatch-logs`
- `@aws-sdk/client-cognito-identity-provider`
- `@aws-sdk/client-s3`
- `@aws-sdk/s3-request-presigner`
- `@faker-js/faker`
- `@fontsource/sarabun`
- `@google-cloud/vertexai`
- `@types/cors`
- `@types/crypto-js`
- `@types/express`
- `@types/express-fileupload`
- `@types/formidable`
- `@types/jest`
- `@types/mssql`
- `@types/node`

## Scripts
- `start`
- `set-alias:dev`
- `set-alias:prod`
- `build`
- `dev`
- `test`
- `test:coverage`

## Structure
```
📄 Deploy-Prod-Summary-20260417.xlsx
📄 Dockerfile
📄 Release-Signoff-Excise-Wine-20260416.xlsx
📄 appspec.yaml
📄 compose.yaml
📁 databases
  📁 docs
    📄 1_introduction_and_objective.md
    📄 2_system_overview.md
    📄 3_knex_workflow.md
    📄 4_erd.md
    📄 5_data_dictionary.md
    📄 6_data_integrity.md
    📄 7_implementation_rollback_plan.md
    📄 8_expected_benefits.md
  📁 migrations
    📄 20260408042033_create_tbft_group_table.js
    📄 20260408042712_create_tbft_feature_table.js
    📄 20260408044419_create_tbft_group_permission_table.js
    📄 20260408044434_create_tbft_user_group_table.js
    📄 20260429000000_add_officer_receipt_to_tbft_cart.js
  📄 readme.md
  📄 run.js
📁 etl
  📁 index-copy
    📄 Dockerfile
    📄 README.md
    📄 main.py
    📄 pyproject.toml
    📄 requirements.txt
    📄 uv.lock
📄 knexfile.js
📁 logs
  📄 error.log
  📄 stdout.log
  📄 warn.log
📄 nodemon.json
📄 package-lock.json
📄 package.json
📁 path
  📁 to
📁 postman
  📄 apiv6.postman_collection.json
  📄 pdf-poc.postman_collection.json
  📄 searchNameByImage.postman_collection.json
📁 public
  📁 fonts
    📁 THSarabunNew
  📄 hello-world.txt
📄 readme.md
📁 scripts
  📄 after_install.sh
  📄 application_start.sh
  📄 before_install.sh
  📄 check-import-purpose-fields.sql
  📄 create-tbftImportItem.sql
  📄 seed-typesense-from-db.ts
  📄 seed-typesense-sub-regions.ts
  📄 set-alias.js
  📄 start.js
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
