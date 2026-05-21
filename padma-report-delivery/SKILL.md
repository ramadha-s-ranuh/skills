---
name: padma-report-delivery
description: Generate a PDF from a Padma Monthly Customer Review Report markdown file, upload the PDF to S3, and send an email with the PDF attached via GL Connectors. PDF-only — no CSV attachment (intentional difference from the CPI variant).
version: 0.1.0
tags:
  - padma
  - pdf
  - email
  - s3
triggers:
  - generate padma monthly review pdf
  - send padma report email
  - upload padma report to s3
  - deliver padma report
---

# Padma Report Delivery

A pre-built script that takes a markdown report file (the Padma Monthly Customer Review Report), generates a PDF, optionally uploads it to S3, and sends an email with the PDF attached — all in one sandbox execution.

## Why it differs from `cpi-report-delivery`

- **PDF-only.** The Padma PoC does not produce a parallel CSV export, so there is no second file to ship. `--csv-path` is therefore removed.
- **Padma defaults.** PDF filename is `report_padma.pdf`; S3 prefix env var is `PADMA_S3_PREFIX`.
- Everything else (Markdown → PDF via WeasyPrint, GL Connectors `google_mail.send_email`, S3 upload via boto3) is identical to the CPI skill.

## Prerequisites

The following must be set in the agent's `sandbox_env` before this skill is invoked:

| Variable | Description |
|---|---|
| `AWS_DEFAULT_REGION` | AWS region for S3 |
| `AWS_ACCESS_KEY_ID` | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |
| `AWS_EXPECTED_BUCKET_OWNER` | (optional) Expected S3 bucket owner account ID |
| `PADMA_S3_BUCKET` | Target S3 bucket (used as `--s3-bucket` fallback) |
| `PADMA_S3_PREFIX` | S3 key prefix (used as `--s3-prefix` fallback) |
| `GL_CONNECTORS_BASE_URL` | GL Connectors API base URL |
| `GL_CONNECTORS_API_KEY` | GL Connectors API key |
| `GL_CONNECTORS_USERNAME` | GL Connectors user identifier (the BOSA user whose `google_mail` integration is the email sender) |
| `GL_CONNECTORS_USER_SECRET` | GL Connectors user secret |

## Input contract

| Argument | Required | Description |
|---|---|---|
| `--markdown-file` | Yes | Absolute shell-side path to the full markdown report (written via `write_file` / `cat >> heredoc`) |
| `--email-to` | Yes (for email) | Comma-separated recipient addresses |
| `--email-subject` | Yes (for email) | Email subject line |
| `--email-body-file` | No | Path to a separate markdown file for the email HTML body; defaults to extracting Sections 0–1 from `--markdown-file` |
| `--s3-bucket` | No | Overrides `PADMA_S3_BUCKET` env var |
| `--s3-prefix` | No | Overrides `PADMA_S3_PREFIX` env var |
| `--skip-s3` | No | Skip S3 upload (for testing) |
| `--skip-email` | No | Skip email send (for testing) |
| `--append-markdown` | No | Append a markdown string to `--markdown-file` and exit |

## Output contract

On success prints a single JSON line to stdout:
```json
{"pdf_path": "/tmp/output/report_padma.pdf", "s3_uris": ["s3://..."], "email_sent": true}
```

On failure exits non-zero with a JSON error on stderr:
```json
{"error": "..."}
```

## Invocation (via `execute` tool)

```bash
python /workspace/skills/padma-report-delivery/deliver.py \
  --markdown-file /workspace/tmp/report.md \
  --email-to "recipient@example.com,other@example.com" \
  --email-subject "Laporan Bulanan Customer Review Padma - April 2026"
```

## Standard agent flow

0. **Pre-flight (REQUIRED, once per sandbox session):** install WeasyPrint and markdown2 via the `execute` tool — the sandbox image does not bundle them by default:

   ```bash
   pip install --quiet weasyprint markdown2
   ```

1. Run report SQL queries via `bosa_sql_query_tool` against the Padma PoC DB.
2. Agent initializes and writes `report.md` in chunks via `cat >> heredoc`.
3. Run this skill (`padma-report-delivery`) with `--markdown-file` set to the report path.
4. Verify `email_sent: true` and capture `s3_uris` from stdout.

## Bundled files

| File | Purpose |
|---|---|
| `deliver.py` | Main script — PDF generation, S3 upload, GL Connectors email |
| `reporting.py` | Markdown → HTML → PDF via WeasyPrint (copy of `cpi-report-delivery/reporting.py`) |

> `reporting.py` is duplicated from the CPI skill on purpose so the Padma skill is a self-contained sandbox payload. Sync changes manually if the CPI variant is updated and the change is also desired here.

## Runtime dependencies

PDF generation uses [WeasyPrint](https://weasyprint.org/), which requires both a Python package and native graphics libraries at runtime.

- **Linux sandbox (production):** the base image already provides Pango/Cairo (`libpango-1.0-0`, `libpangoft2-1.0-0`), but `weasyprint` itself is **not** pre-installed. The agent must `pip install --quiet weasyprint markdown2` once per sandbox session before invoking this skill — see step 0 of the Standard agent flow.
- **macOS (local testing):** `brew install pango`, `pip install weasyprint markdown2`, then run with `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` exported.
