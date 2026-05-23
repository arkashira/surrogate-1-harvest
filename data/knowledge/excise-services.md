# Excise Services — Detailed Reference

> API endpoints, models, schemas, Docker configs for all Excise services. Updated: 2026-04-16

---

## excise-wine-nodejs-api

**Tech**: Node.js 22, TypeScript, Express 5.1, MSSQL, Sequelize 6.37
**Entry**: `src/index.ts` | **Port**: 3000

### Key Directories
```
src/
├── app/routes/          # API versions (apiv2-v6)
├── app/controller/      # Controllers
├── app/services/        # Business logic
├── app/repositories/    # Data access
├── app/middleware/       # auth, tracker, tbitHandler, tbitBoot
├── app/reusable/        # Shared types, DB utilities
├── app/config/          # AWS, Cognito, Sequelize, API config
└── app/request/         # Request body types/schemas
```

### API Routes
- `/api/v6/` — Main (health, wineSearch, cart, dashboard)
- `/apiv2` - `/apiv5` — Legacy versions
- Dev zone routes for development

### Key Dependencies
express, cors, @aws-sdk/client-cognito-identity-provider, @aws-sdk/client-s3, mssql, sequelize, firebase-admin, jsonwebtoken, winston, typesense, algolia

### Middleware
- `tracker.ts` — Request/error logging
- `auth.ts` — JWT authentication
- `tbitHandler.ts` — Custom request handling
- `setApiVersion.ts` — API version management

### Environment Variables
JWT_REFRESH_SECRET, JWT_SECRET, JWT_SECRET_RETAIL, JWT_EXPIRATION_IN, DB_HOST/PORT/USER/PASS/NAME, ALGOLIA_ID/KEY, STORAGE_BUCKET_NAME/REGION, REDIS_HOST/PORT, SERVICE_ACCOUNT_JSON

### Docker
Multi-stage: base -> deps -> build -> final (Node 22-alpine)

---

## excise-wine-go-api

**Tech**: Go 1.24, Gorilla Mux, Firestore (GCP)
**Entry**: `cmd/local/main.go` | **Port**: 8080

### Key Directories
```
cmd/local/         # Entry point
api/functions/     # Handlers (wine, dashboard, auth, user, OCR, report)
api/common/        # Common utilities, DB init
pkg/               # Core packages
├── log/           # Logging
├── middleware/     # Logging, CORS
├── database/      # DB operations
├── cognito/       # Cognito auth
├── restful/       # REST utilities
└── rmdb/          # DB connection management
env/               # Environment config files
```

### API Routes
| Route | Purpose |
|-------|---------|
| `/api/develop/*` | Dev endpoints (IAM, showStruct, connection) |
| `/api/master/*` | Master data (titlelist, enumlist, countrylist, filters) |
| `/api/auth/*` | Auth (signup, signin, resetpassword, changepassword) |
| `/api/wine/*` | Wine ops (search, autocomplete, favorites, profile, merchant) |
| `/api/dashboard/*` | Dashboard data |
| `/api/user/*` | User management |
| `/api/usergroup/*` | User groups |
| `/api/noti/*` | Notifications |
| `/api/report/*` | Reports |
| `/api/ocr/*` | OCR (AWS detectDocumentText) |
| `/health` | Health check |

### Middleware
- LoggingMiddleware — Request logging
- CorsMiddleware — CORS + body size limits (50MB)

### Docker
Multi-stage: golang 1.24 builder -> debian slim runtime

---

## excise-wine-authen

**Tech**: Node.js, TypeScript, AWS Lambda, Cognito
**Entry**: `lambda/src/index.ts`
**Purpose**: Cognito User Migration Trigger (Firebase -> Cognito)

### Migration Logic
Handles two trigger sources:

1. **UserMigration_Authentication** (sign-in)
   - Verify credentials against Firebase (tries both projects)
   - Projects: `tbit-exciseft` (Node.js API), `tbit-excise` (Go API)
   - Creates user in Cognito with email_verified, custom:firebaseUid

2. **UserMigration_ForgotPassword** (password reset)
   - Verify email exists in Firebase
   - Allow password reset flow in Cognito

### Key Functions
- `verifyFirebaseUser()` — Authenticate against Firebase
- `getFirebaseUserProfile()` — Fetch user profile
- `checkFirebaseEmailExists()` — Check email for password reset

### Environment Variables
FIREBASE_API_KEY, FIREBASE_API_KEY_SECONDARY

### Dependencies
@aws-sdk/client-cognito-identity-provider, @types/aws-lambda, TypeScript 5.7

---

## excise-wine-proxy

**Tech**: Nginx, SSL/TLS, HTTP/2
**Purpose**: API gateway and reverse proxy

### Upstream Routes

| Path | Destination | Purpose |
|------|-------------|---------|
| `/` | excise-wine-nodejs-api.devthinkbit.com | Node.js API |
| `/cloud/apiv2-retail_comparePice/` | Node.js API | Compare Price |
| `/intra/` | 61.19.233.53 (on-premise) | Intranet relay to api-taitaxes.excise.go.th |
| `/stamp/` | on-premise gateway | stamp2.excise.go.th |

### SSL Config
- TLSv1.2, TLSv1.3
- Strong ciphers (AES-GCM, CHACHA20-POLY1305)
- HSTS enabled
- Domain: `winefasttrack.excise.go.th`

### Proxy Settings
- Client body: 50MB | Timeouts: 600s (connect/read/send)
- Buffer: 128k, 32x4k | HTTP/2, keep-alive
- X-Real-IP, X-Forwarded-For headers

### Config Files
- `aws/prod/nginx/nginx.conf` — Main config
- `aws/prod/nginx/default.conf` — Server blocks
- `aws/prod/nginx/Dockerfile` — Build

---

## excise-car-backend

**Tech**: Node.js 20.12.2, TypeScript, Express 4.21, Prisma 5.22, MSSQL
**Entry**: `src/index.ts` | **Port**: 3000 | **Version**: 2.2.22

### Key Directories
```
src/app/routes/v2/              # API routes (car, brand, cart, dashboard, imports)
src/app/routes/gateway_web_services/  # GWS integration
src/app/models/                 # Database models
src/app/middleware/              # Logging, auth, health, API docs
src/app/providers/              # HTTP, logger, Firebase, AsyncHandler
src/bootstrap/                  # Sequelize, Firebase init
prisma/                         # Schema and migrations
cron/                           # Lambda cron functions
```

### API Routes (v2)
`/car/v2/car/*`, `/car/v2/brand/*`, `/car/v2/cart/*`, `/car/v2/imports/*`, `/car/v2/users/*`, `/car/v2/dashboard/*`, `/car/v2/permission/*`, `/car/v2/model/*`, `/car/v2/body-style`, `/car/v2/config`, `/gwws/*`

### Prisma Schema (SQL Server)
| Model | Purpose |
|-------|---------|
| `cart` | Cart header (req_no, status, created/updated_at/by, lock_until) |
| `cart_import` | Import details (type_car, excise_no, objective, attachments) |
| `cart_attach` | Cart attachments (url, file_type) |
| `cart_item` | Line items (car_id, selling_price, excise_tax, local_tax) |
| `cart_item_desc` | Item descriptions |
| `mas2_car` | Master car data |
| `users` | User records |
| `tbl_currency` | Currency data |

### Key Dependencies
express, cors, helmet, @prisma/client, firebase-admin, mssql, sequelize, winston, swagger-ui-express, zod

### Environment Variables
NODE_ENV, PORT, DATABASE_URL, DB_HOST/NAME/PASSWORD/USER, JWT_SECRET/REFRESH_SECRET/EXPIRATION, S3_BUCKET/REGION, FIREBASE_*, GOOGLE_APPLICATION_CREDENTIALS

### Docker
Multi-stage: node:20.12.2-alpine, Prisma generate, Firebase credentials injection

---

## excise-car-cron / excise-car-cron-staging

**Tech**: Node.js, TypeScript, Prisma, Webpack, AWS Lambda
**Purpose**: Scheduled Lambda functions

### Handlers

1. **getCurrencyHandler**
   - Scrapes Thai Customs website (`customs.go.th`) for exchange rates
   - Parses HTML with Cheerio
   - Normalizes Thai headers to English (import, export, code, country)
   - Updates `tbl_currency` table via Prisma
   - Runs on schedule

2. **autoRejectionHandler**
   - Placeholder/TODO for auto-rejection logic

### Dependencies
@prisma/client, axios, cheerio, dayjs

### Build
TypeScript -> ts-loader -> Webpack -> `/dist/index.js`

---

## Additional Services

### excise-wine-api (Legacy)
**Tech**: Node.js 22, TypeScript, Express 5.1, MSSQL/Sequelize, Vertex AI, Algolia
**Entry**: `src/index.ts` | **Port**: 3000
**Status**: Legacy — being superseded by excise-wine-nodejs-api

### excise-wine-python-api (Deprecated)
**Tech**: Python 3.13+, Flask[async], Algolia 4.35, Redis, Hypercorn, Flasgger
**Entry**: `server/app.py`
**Status**: DEPRECATED — fallback search service only

### excise-wine-frontend
**Tech**: React, Vite/CRA, Nginx hosting
**Structure**: `src/`, `public/`, `dist/`, `e2e/`, `nginx-hosting/`

### excise-wine-fasttrack-frontend
**Tech**: React (same as main frontend, fast-track variant)

### excise-wine-fasttrack-mobile
**Tech**: Flutter (Dart), Fastlane (iOS/Android automation)
**Platforms**: iOS, Android, Web, Linux, macOS, Windows
**Structure**: `lib/`, `ios/`, `android/`, `web/`, `fastlane/`, `assets/`

### elephant (Car Dev Variant)
**Tech**: Node.js, Express 5.2.1, TypeScript 5.9.3, Prisma 7.2.0, MSSQL
**Purpose**: Dev/testing variant of excise-car-backend with latest dependencies
**Version**: 2.2.13

---

## Summary Matrix

| Service | Language | Framework | Database | Port | Deployment |
|---------|----------|-----------|----------|------|-----------|
| wine-nodejs-api | TypeScript | Express 5.1 | MSSQL/Sequelize | 3000 | Docker ECS |
| wine-go-api | Go 1.24 | Gorilla Mux | Firestore | 8080 | Docker ECS |
| wine-authen | TypeScript | Lambda | Firebase/Cognito | - | Lambda |
| wine-proxy | Nginx | Nginx | - | 80/443 | EC2 CodeDeploy |
| wine-api (legacy) | TypeScript | Express 5.1 | MSSQL/Sequelize | 3000 | Docker ECS |
| wine-python-api | Python | Flask | Algolia/Redis | - | ECS Fargate |
| wine-frontend | React | Vite/CRA | - | 80 | Nginx/S3 |
| wine-fasttrack-mobile | Dart | Flutter | - | - | App Store/Play |
| car-backend | TypeScript | Express 4.21 | MSSQL/Prisma | 3000 | Docker ECS |
| car-cron | TypeScript | Lambda | MSSQL/Prisma | - | Lambda |
| elephant (car dev) | TypeScript | Express 5.2 | MSSQL/Prisma 7.2 | 3000 | Local |
