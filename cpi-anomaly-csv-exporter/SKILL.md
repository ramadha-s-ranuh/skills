---
name: cpi-anomaly-csv-exporter
description: Convert a BOSA SQL query result file into the canonical CPI anomaly CSV (anomaly_data.csv) without inlining rows into the model context. Use whenever the anomaly query result is large (auto-evicted to /tool_outputs/<id>.txt) or whenever you need to bypass csv_data inlining for cpi_report_tool.
version: 0.1.0
tags:
  - cpi
  - csv
  - anomaly
triggers:
  - generate anomaly csv
  - export anomaly data
  - cpi anomaly export
---

# CPI Anomaly CSV Exporter

A pre-built script that turns a `bosa_sql_query_tool` result (saved as a file by the agent filesystem auto-eviction) into the canonical 9-column anomaly CSV expected by the CPI reporting workflow.

## When to use

Run this skill instead of passing `csv_data` inline to `cpi_report_tool` when:

- The anomaly SQL result was auto-evicted to `/tool_outputs/<tool_call_id>.txt` because it exceeded the model context budget.
- You want to handle the full unbounded result set (no `LIMIT 500`) without spending tokens.

## Input contract

The script accepts a single positional argument: the path to a file containing the JSON output of `bosa_sql_query_tool`.

Accepted JSON shapes (the script auto-detects):

- `[{...row...}, ...]`
- `{"data": [...]}`
- `{"rows": [...]}`
- `{"result": [...]}` / `{"results": [...]}`
- `{"success": true, "data": [...]}`
- A raw eviction `.txt` whose content is one of the above.

Each row should expose at least: `anomaly_case_id`, one of (`use_case`/`use_case_name`/`use_case_code`), `entity_name`, `entity_id`, `event_date`, one of (`type`/`reason_name`/`reason_type`), one of (`reason_anomaly`/`reason_text`).

## Output contract

The script writes a UTF-8 CSV with this exact header order:

1. `anomaly_case_id`
2. `use_case`
3. `entity_name`
4. `entity_id`
5. `event_date`
6. `type`
7. `reason_anomaly`
8. `perlu_di_follow_up(Y/N)` — defaulted to `Y`
9. `note` — defaulted to empty string

Default output path is `/tmp/output/anomaly_data.csv` (the script's argv default). In practice **always pass an explicit second argument** of `/workspace/tmp/output/anomaly_data.csv` so `cpi_report_tool` can find the file (see the path rule below).

## Invocation (via `execute` tool)

```bash
python /workspace/skills/cpi-anomaly-csv-exporter/export.py <input_file_path> [output_csv_path] [--s3-uri s3://bucket/key]
```

When `--s3-uri` is supplied the script also uploads the written CSV to S3, using AWS credentials from environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` — set on the sandbox by the agent).

The script prints a single JSON line to stdout: `{"csv_path": "...", "row_count": N, "s3_uri": "..."}` (the `s3_uri` field is present only when `--s3-uri` was passed). On success exit code is `0`; on failure non-zero with a JSON error on stderr.

> ⚠️ **Why uploading to S3 is required, not optional:**
>
> The sandbox has *three* isolated filesystems and `cpi_report_tool` runs in the third one (a custom-tool runtime that does not share storage with the shell or with the SDK-virtualized `/workspace/`). Local-path bridges (e.g., `/tmp/output/...` or `/workspace/tmp/output/...`) cannot reach the tool's runtime. S3 is the supported transport.
>
> Path rule for inputs you still pass through the shell (the auto-evicted result file): prepend `/workspace`.
>
> | Path as SDK tools see it | Path to use in `execute` |
> | --- | --- |
> | `/skills/...` | `/workspace/skills/...` |
> | `/tool_outputs/<tool_call_id>.txt` | `/workspace/tool_outputs/<tool_call_id>.txt` |
> | `/tmp/anomaly_input.json` (written via `write_file`) | `/workspace/tmp/anomaly_input.json` |

## Standard agent flow

1. Run the anomaly SQL via `bosa_sql_query_tool` **without** `LIMIT 500` (the full set).
2. If the tool result exceeds the eviction threshold (~80K chars), the SDK saves it to `/tool_outputs/<tool_call_id>.txt` (SDK-virtualized path) and the agent receives a preview message containing that path. **Translate to shell path: `/workspace/tool_outputs/<tool_call_id>.txt`.**
3. Invoke this skill with shell-side input path **and `--s3-uri`** — the sandbox already has `AWS_*` and `CPI_S3_*` env vars set, so just construct the URI in-shell:
   ```bash
   python /workspace/skills/cpi-anomaly-csv-exporter/export.py \
     /workspace/tool_outputs/<tool_call_id>.txt \
     /workspace/tmp/output/anomaly_data.csv \
     --s3-uri "s3://${CPI_S3_BUCKET}/${CPI_S3_STAGING_PREFIX}/$(date +%Y%m%d_%H%M%S)/anomaly_data.csv"
   ```
4. Verify the printed `row_count` matches the SQL `COUNT(*)` (run a small `SELECT COUNT(*)` in parallel to validate). **Capture the printed `s3_uri`** — Step B requires it.
5. Call `cpi_report_tool` with `csv_data=[]` AND `csv_s3_uri="<the s3_uri printed in step 4>"` — the tool downloads the CSV from S3 and saves it into its own output directory, where `s3_upload_tool` and `cpi_email_sender_tool` can read it. The `csv_message` will start with `"CSV downloaded from"`.
6. Continue with `s3_upload_tool` and `cpi_email_sender_tool` exactly as before.

## Why a skill (not a tool)

A tool roundtrip would still send the full result back through the model. The script in this skill runs entirely on the agent filesystem, reading and writing files directly — no rows ever cross the model context.
