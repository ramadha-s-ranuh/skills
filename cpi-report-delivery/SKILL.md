---
name: cpi-report-delivery
description: Generate a PDF from a markdown report file, upload PDF and CSV to S3, and send an email with both files attached via GL Connectors. Replaces cpi_report_tool + s3_upload_tool + cpi_email_sender_tool. Runs entirely in the agent sandbox so no S3 bridge is needed between tools.
version: 0.1.0
tags:
  - cpi
  - pdf
  - email
  - s3
triggers:
  - generate pdf report
  - send cpi report email
  - upload cpi report to s3
  - deliver cpi report
---

# CPI Report Delivery

A pre-built script that takes a markdown report file and an anomaly CSV (written by `cpi-anomaly-csv-exporter`), generates a PDF, uploads both to S3, and sends an email with both files attached — all in one sandbox execution.

## Why a skill (not tools)

Previously the pipeline used three BaseTool calls (`cpi_report_tool` → `s3_upload_tool` → `cpi_email_sender_tool`), each running in an isolated custom-tool runtime with no shared filesystem. An S3 staging step was required to bridge the CSV from the shell sandbox to the tool runtime.

This skill runs in the **same sandbox** as the shell, so:
- No S3 staging URI needed — the CSV written by `cpi-anomaly-csv-exporter` is directly readable.
- No filesystem isolation to work around.
- One `execute` call replaces three tool calls.

## Prerequisites

The following must be set in the agent's `sandbox_env` before this skill is invoked:

| Variable | Description |
|---|---|
| `AWS_DEFAULT_REGION` | AWS region for S3 |
| `AWS_ACCESS_KEY_ID` | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |
| `AWS_EXPECTED_BUCKET_OWNER` | (optional) Expected S3 bucket owner account ID |
| `CPI_S3_BUCKET` | Target S3 bucket (used as `--s3-bucket` fallback) |
| `CPI_S3_PREFIX` | S3 key prefix (used as `--s3-prefix` fallback) |
| `GL_CONNECTORS_BASE_URL` | GL Connectors API base URL |
| `GL_CONNECTORS_API_KEY` | GL Connectors API key |
| `GL_CONNECTORS_USERNAME` | GL Connectors user identifier |
| `GL_CONNECTORS_USER_SECRET` | GL Connectors user secret |

## Input contract

| Argument | Required | Description |
|---|---|---|
| `--csv-path` | Yes | Absolute shell-side path to the anomaly CSV (output of `cpi-anomaly-csv-exporter`) |
| `--markdown-file` | Yes | Absolute shell-side path to the full markdown report (written via `write_file`) |
| `--email-to` | Yes (for email) | Comma-separated recipient addresses |
| `--email-subject` | Yes (for email) | Email subject line |
| `--email-body-file` | No | Path to a separate markdown file for the email HTML body; defaults to `--markdown-file` |
| `--s3-bucket` | No | Overrides `CPI_S3_BUCKET` env var |
| `--s3-prefix` | No | Overrides `CPI_S3_PREFIX` env var |
| `--skip-s3` | No | Skip S3 upload (for testing). **TEMPORARY: defaults to true — S3 archival is currently disabled.** |
| `--skip-email` | No | Skip email send (for testing) |
| `--append-markdown` | No | Append a markdown string to `--markdown-file` and exit (used by agent to write report chunks without a separate write tool) |

## Output contract

On success prints a single JSON line to stdout:
```json
{"pdf_path": "/tmp/output/report_cpi.pdf", "csv_path": "...", "s3_uris": ["s3://...", "s3://..."], "email_sent": true}
```

On failure exits non-zero with a JSON error on stderr:
```json
{"error": "..."}
```

## Invocation (via `execute` tool)

```bash
python /workspace/skills/cpi-report-delivery/deliver.py \
  --csv-path /workspace/tmp/output/anomaly_data.csv \
  --markdown-file /workspace/tmp/report.md \
  --email-to "recipient@example.com,other@example.com" \
  --email-subject "Laporan Outsourcing CPI Maret 2026"
```

### With separate email body (Section 1 only)

```bash
python /workspace/skills/cpi-report-delivery/deliver.py \
  --csv-path /workspace/tmp/output/anomaly_data.csv \
  --markdown-file /workspace/tmp/report.md \
  --email-body-file /workspace/tmp/email_body.md \
  --email-to "recipient@example.com" \
  --email-subject "Laporan Outsourcing CPI Maret 2026"
```

## Standard agent flow

0. **Pre-flight (REQUIRED, once per sandbox session):** install WeasyPrint and markdown2 via the `execute` tool — the sandbox image does not bundle them by default:

   ```bash
   pip install --quiet weasyprint markdown2
   ```

   If this step is skipped, `deliver.py` will exit with `Could not import bundled reporting module: No module named 'weasyprint'` or `markdown2`.
1. Run anomaly SQL via `bosa_sql_query_tool` (no `LIMIT`).
2. Run `cpi-anomaly-csv-exporter` skill → writes `/workspace/tmp/output/anomaly_data.csv`.
3. Agent initializes and writes `report.md` in chunks using Python heredoc via the `execute` tool (shell-side path: `/workspace/tmp/report.md`). Each chunk:
   ```bash
   python3 << 'CPI_CHUNK_EOF'
   with open('/workspace/tmp/report.md', 'a', encoding='utf-8') as f:
       f.write("""<markdown chunk content>
   """)
   CPI_CHUNK_EOF
   ```
4. Run this skill (`cpi-report-delivery`) with the paths from steps 2–3.
5. Verify `email_sent: true` and capture `s3_uris` from stdout.

## Bundled files

| File | Purpose |
|---|---|
| `deliver.py` | Main script — PDF generation, S3 upload, GL Connectors email |
| `reporting.py` | Markdown → HTML → PDF via WeasyPrint (copy of `cpi/utils/reporting.py`) |

> ⚠️ `reporting.py` is a copy of `cpi/utils/reporting.py`. If `cpi/utils/reporting.py` is updated, sync the copy here as well.

## Runtime dependencies

PDF generation uses [WeasyPrint](https://weasyprint.org/), which requires both a
Python package and native graphics libraries at runtime.

- **Linux sandbox (production):** the base image already provides Pango/Cairo
  (`libpango-1.0-0`, `libpangoft2-1.0-0`), but `weasyprint` itself is **not**
  pre-installed. The agent **must** run `pip install --quiet weasyprint` via the
  `execute` tool once per sandbox session before invoking this skill — see
  step 0 of the Standard agent flow above.
- **macOS (local testing):** `brew install pango`, `pip install weasyprint`,
  then run with `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` exported.

Failure modes:
- Missing Python package → `Could not import bundled reporting module: No module named 'weasyprint'`.
- Missing Pango/Cairo at runtime → `PDF generation failed`.
