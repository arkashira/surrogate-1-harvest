# Privacy Policy

> ROADMAP-100 #94. Plain-language privacy policy for surrogate-1.
> **Effective**: 2026-05-02. **Last updated**: 2026-05-02.

surrogate-1 is a free, dev/test-tier service. We aim to collect as
little as possible.

## 1. What we collect

| Data | Source | Why | Retention |
|---|---|---|---|
| Request metadata | Cloudflare edge logs (IP, UA, path, timestamp) | Abuse prevention, debugging | 30 days, then aggregated |
| Auth token id | `X-Auth-Token` matched against secret | Access control | Not stored — only matched at request time |
| Cursor + dataset payloads | API requests to `/cursor/*`, `/datasets`, `/tasks/push` | Persisted product data (this is the service) | Until you delete it via API |
| Audit log | Worker writes on mutation | Compliance + debugging | 90 days |
| Workers AI prompt + completion | `/ai/<model>` proxy | Forwarded to Cloudflare; not stored by us | Per Cloudflare policy |
| LLM upstream calls | Daemons → Groq/Cerebras/etc. | Pipeline operation | Per upstream provider |

## 2. What we DO NOT collect

- We do **not** collect names, emails, addresses, phone numbers, or
  payment info — there is no signup and no payment.
- We do **not** collect cookies for advertising.
- We do **not** sell, rent, or barter any data.
- We do **not** profile users.
- We do **not** intentionally collect PII. If you submit PII as content,
  treat that as your decision.

## 3. PII statement

This service is **dev/test only**. No PII is intentionally collected.
**Do not submit personal data, customer data, or any data subject to
GDPR / HIPAA / PCI-DSS / PDPA / similar regulations.** If you mistakenly
submit such data, contact us at `hermes@axentx.ai` and we will purge it
on a best-effort basis.

## 4. Third-party processors

The service runs on top of:

- **Cloudflare** (Workers, D1, KV, Queues, Vectorize, Pages, AI, R2)
- **Hugging Face** (model + dataset hosting; Spaces hosting)
- **Supabase** (managed Postgres for the work queue)
- **Google Cloud** (e2-micro VM hosting the daemons)
- **LLM providers** (Groq, Cerebras, OpenRouter, Google Gemini, Together,
  Fireworks, Mistral, DeepSeek, OpenAI, Anthropic, Workers AI — order
  rotates)

Each has their own privacy practices. By using surrogate-1 you also
accept the privacy practices of whichever processors handle your
request.

## 5. Cookies + tracking

- The static dashboard at `/dash` does not set cookies.
- No analytics SDKs (no GA, no Mixpanel, no Plausible-self-hosted, etc.).
- Server-side request counters are aggregated, not per-user.

## 6. Children

The service is not directed at children under 13. We do not knowingly
collect data from children under 13. If you are under 13, do not use
the service.

## 7. Your rights

- **Access**: ask us at `hermes@axentx.ai` what we have stored related
  to a given dataset slug or auth token.
- **Deletion**: delete cursor / dataset rows yourself via API, or ask
  us. We delete within 30 days of a verified request.
- **Portability**: you already have the data — it's whatever you
  submitted to `/datasets` and `/cursor/*/advance`. Export via `GET`.
- **Complaints**: contact us first. If unresolved, you may complain to
  the Personal Data Protection Committee (PDPC, Thailand) or your local
  data-protection authority.

## 8. Security

- Auth token at rest is a Cloudflare Worker secret (encrypted).
- All endpoints are HTTPS-only.
- We do not store payment info because we do not collect it.
- We follow industry-standard practice but cannot guarantee absolute
  security. See the Terms of Service §5–6 for limitations.

## 9. Changes

We may update this policy by editing this file. The commit timestamp on
this file is authoritative.

## 10. Contact

`hermes@axentx.ai`
