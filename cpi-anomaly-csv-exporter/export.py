"""Convert anomaly data into the canonical CPI anomaly CSV.

Supports three input modes:

1. **File mode** (default): reads a JSON payload written by ``sql_query_tool``
   (inline result or auto-evicted ``/tool_outputs/<id>.txt``) and converts it to CSV.

2. **GL Connector mode** (``--gl-connector`` + ``--pg-query``): runs the SQL through
   the GL Connectors SDK. This mode works under sandbox network
   restrictions that block raw PostgreSQL TCP (port 5438). Credentials come
   from the standard ``GL_CONNECTORS_*`` env vars.

Usage:
    # File mode
    python export.py <input_file> [output_csv] [--s3-uri s3://bucket/key]

    # GL Connector mode (sandbox-safe, no positional input_file)
    python export.py --gl-connector --pg-query <sql> [output_csv] [--s3-uri ...]

On success prints a single JSON line to stdout:
    {"csv_path": "...", "row_count": N}          (or with "s3_uri" when uploaded)

On failure exits non-zero with a JSON error on stderr.

Author:
    Jordan Hakiki Sipahutar (jordan.h.sipahutar@gdplabs.id)

References:
    https://gdplabs.gitbook.io/sdk/gl-ai-agent-package/guides/skills
    https://gdplabs.gitbook.io/sdk/gl-ai-agent-package/guides/agent-filesystem#tool-output-auto-eviction
"""

from __future__ import annotations

import ast
import csv
import json
import os
import subprocess
import sys
from typing import Any

CANONICAL_HEADERS: list[str] = [
    "anomaly_case_id",
    "use_case",
    "entity_name",
    "entity_id",
    "event_date",
    "type",
    "reason_anomaly",
    "perlu_di_follow_up(Y/N)",
    "note",
]

COLUMN_ALIASES: dict[str, str] = {
    "use_case_name": "use_case",
    "use_case_code": "use_case",
    "reason_name": "type",
    "reason_type": "type",
    "reason_text": "reason_anomaly",
    "reason_anomaly_text": "reason_anomaly",
}

DEFAULTS: dict[str, str] = {
    "perlu_di_follow_up(Y/N)": "Y",
    "note": "",
}

import tempfile

DEFAULT_OUTPUT_PATH = os.path.join(tempfile.gettempdir(), "output", "anomaly_data.csv")

ROW_LIST_KEYS: tuple[str, ...] = ("data", "rows", "result", "results", "items", "records")


def _fail(message: str, exit_code: int = 1) -> None:
    """Print a JSON error to stderr and exit."""
    sys.stderr.write(json.dumps({"error": message}) + "\n")
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# GL Connector mode helpers
# ---------------------------------------------------------------------------

GL_REQUIRED_ENV: tuple[str, ...] = (
    "GL_CONNECTORS_BASE_URL",
    "GL_CONNECTORS_API_KEY",
    "GL_CONNECTORS_USERNAME",
    "GL_CONNECTORS_USER_SECRET",
    "GL_CONNECTORS_SQL_IDENTIFIER",
)


def _gl_env() -> tuple[str, str, str, str, str]:
    """Read GL Connectors env vars or fail with a clear error message."""
    base_url = os.environ.get("GL_CONNECTORS_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("GL_CONNECTORS_API_KEY", "")
    username = os.environ.get("GL_CONNECTORS_USERNAME", "")
    secret = os.environ.get("GL_CONNECTORS_USER_SECRET", "")
    identifier = os.environ.get("GL_CONNECTORS_SQL_IDENTIFIER", "")
    missing = [name for name in GL_REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        _fail(f"Missing required GL Connectors env vars: {', '.join(missing)}")
    return base_url, api_key, username, secret, identifier


def _query_gl_connector(query: str, timeout: int = 180) -> list[dict[str, Any]]:
    """Run ``query`` via the GL Connectors SDK.

    Returns:
        list[dict[str, Any]]: All result rows as plain dicts.
    """
    try:
        from gl_connectors_sdk import GLConnectors  # noqa: PLC0415
    except ImportError as exc:
        _fail(f"gl_connectors_sdk is required for --gl-connector but is not installed: {exc}")

    base_url, api_key, username, secret, identifier = _gl_env()
    connector = GLConnectors(api_base_url=base_url, api_key=api_key)
    
    try:
        user = connector.authenticate(username, secret)
    except Exception as exc:
        _fail(f"Failed to authenticate with GL Connectors: {exc}")

    if user and not connector.user_has_integration("sql", user.token):
        _fail(f"User lacks SQL integration. Please auth here: {connector.initiate_connector_auth('sql', user.token)}")

    params = {"query": query}
    try:
        result, _ = connector.execute(
            "sql", "query", 
            token=user.token, 
            max_attempts=1, 
            input_=params, 
            identifier=identifier,
            timeout=timeout
        )
    except Exception as exc:
        _fail(f"GL Connector SQL action failed: {exc}")

    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(result, dict):
        if "data" in result and isinstance(result["data"], list):
            return [row for row in result["data"] if isinstance(row, dict)]
        for key in ("rows", "data_rows", "items"):
            value = result.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
            
    _fail(f"GL Connector SQL response did not contain a row list: {str(result)[:400]}")
    return []  # unreachable


# ---------------------------------------------------------------------------
# File-mode helpers
# ---------------------------------------------------------------------------


def _load_payload(path: str) -> Any:
    """Read the file at ``path`` and return the parsed payload.

    Tries JSON first, then Python literal (the format ``bosa_sql_query_tool``
    emits and that the eviction middleware persists verbatim — single-quoted
    dicts and bare ``True``/``False``/``None``). Tolerates leading non-payload
    preamble by retrying from each ``[`` or ``{`` boundary.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        _fail(f"unable to read input file {path!r}: {exc}")

    raw = raw.strip()
    if not raw:
        _fail(f"input file {path!r} is empty")

    parsed, error = _try_parse(raw)
    if parsed is not None:
        return parsed

    for offset in _bracket_offsets(raw):
        parsed, error = _try_parse(raw[offset:])
        if parsed is not None:
            return parsed

    _fail(f"failed to parse payload from {path!r}: {error}")
    return None


def _try_parse(text: str) -> tuple[Any, str | None]:
    """Attempt JSON then Python literal parsing on ``text``.

    Returns:
        tuple[Any, str | None]: ``(parsed, None)`` on success or
        ``(None, last_error_message)`` on failure.
    """
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        json_error = str(exc)
    try:
        return ast.literal_eval(text), None
    except (ValueError, SyntaxError) as exc:
        return None, f"json={json_error}; literal_eval={exc}"


def _bracket_offsets(text: str) -> list[int]:
    """Return all ``[``/``{`` indices in ``text``, in order."""
    return [i for i, ch in enumerate(text) if ch in "[{"]


def _is_row_list(value: Any) -> bool:
    """Return True if ``value`` is a list whose first element (if any) is a dict."""
    return isinstance(value, list) and (not value or isinstance(value[0], dict))


def _filter_row_list(value: list[Any]) -> list[dict[str, Any]]:
    """Return only the dict elements from ``value``."""
    return [row for row in value if isinstance(row, dict)]


def _search_dict_for_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Search a dict payload for the first valid list of row dicts.

    First checks well-known keys, then falls back to scanning all values
    (including nested dicts via recursion).
    """
    for key in ROW_LIST_KEYS:
        value = payload.get(key)
        if _is_row_list(value):
            return _filter_row_list(value)

    for value in payload.values():
        if _is_row_list(value) and value:
            return _filter_row_list(value)
        if isinstance(value, dict):
            nested = _extract_rows(value)
            if nested:
                return nested

    return []


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    """Walk the parsed payload and return the first list of dict rows found."""
    if _is_row_list(payload):
        return _filter_row_list(payload)

    if isinstance(payload, dict):
        rows = _search_dict_for_rows(payload)
        if rows:
            return rows

    _fail("could not locate a list of row dicts in the payload")
    return []


# ---------------------------------------------------------------------------
# Shared write / upload helpers
# ---------------------------------------------------------------------------


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Apply alias mapping and fill defaults for one row."""
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        target = COLUMN_ALIASES.get(key, key)
        if target not in normalized or normalized[target] in (None, ""):
            normalized[target] = value
    for key, default in DEFAULTS.items():
        normalized.setdefault(key, default)
    return normalized


def _write_csv(rows: list[dict[str, Any]], output_path: str) -> None:
    """Write the canonical CSV to ``output_path``."""
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CANONICAL_HEADERS)
        writer.writeheader()
        for row in rows:
            normalized = _normalize_row(row)
            writer.writerow({key: normalized.get(key, "") for key in CANONICAL_HEADERS})


def _upload_to_s3(local_path: str, s3_uri: str) -> None:
    """Upload ``local_path`` to ``s3_uri`` using boto3.

    Reads AWS credentials from standard environment variables
    (``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``, ``AWS_DEFAULT_REGION``
    or ``AWS_REGION``). Fails the script if upload errors out.
    """
    if not s3_uri.startswith("s3://"):
        _fail(f"--s3-uri must start with s3://, got {s3_uri!r}")
    bucket_key = s3_uri[len("s3://") :]
    if "/" not in bucket_key:
        _fail(f"--s3-uri must include a key, got {s3_uri!r}")
    bucket, key = bucket_key.split("/", 1)

    try:
        import boto3  # noqa: PLC0415  -- imported lazily so the script runs without boto3 when --s3-uri is omitted
    except ImportError as exc:
        _fail(f"boto3 is required for --s3-uri but is not installed: {exc}")

    region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")
    expected_bucket_owner = os.environ.get("AWS_EXPECTED_BUCKET_OWNER", "")
    client_kwargs: dict[str, Any] = {}
    if region:
        client_kwargs["region_name"] = region
    extra_args: dict[str, Any] = {}
    if expected_bucket_owner:
        extra_args["ExpectedBucketOwner"] = expected_bucket_owner
    try:
        boto3.client("s3", **client_kwargs).upload_file(
            local_path, bucket, key, ExtraArgs=extra_args if extra_args else None
        )
    except Exception as exc:
        _fail(f"S3 upload to {s3_uri} failed: {exc}")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(
    argv: list[str],
) -> tuple[str | None, str, str | None, str | None, bool]:
    """Parse CLI args for file mode and GL Connector mode.

    Returns:
        tuple: ``(input_path, output_path, s3_uri, pg_query, gl_connector)``

        - ``input_path`` is ``None`` outside file mode.
        - ``pg_query`` is required in GL Connector mode.
        - ``gl_connector`` is ``True`` when the GL Connector mode was requested.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert anomaly data into the canonical CPI anomaly CSV.",
        epilog=f"default output: {DEFAULT_OUTPUT_PATH}",
    )
    parser.add_argument("--s3-uri", dest="s3_uri", help="S3 URI to upload the output CSV")
    parser.add_argument("--pg-query", dest="pg_query", help="SQL query string")
    parser.add_argument("--gl-connector", dest="gl_connector", action="store_true", help="Run SQL via GL Connectors SDK")
    parser.add_argument("positionals", nargs="*", help="input_file and/or output_csv depending on mode")

    args = parser.parse_args(argv[1:])

    input_path: str | None = None
    output_path: str = DEFAULT_OUTPUT_PATH

    if args.gl_connector:
        if not args.pg_query:
            _fail("--gl-connector requires --pg-query")
        if args.positionals:
            output_path = args.positionals[0]
    elif not args.positionals:
        parser.print_usage(sys.stderr)
        sys.exit(2)
    else:
        if args.pg_query:
            _fail("--pg-query requires --gl-connector")
        input_path = args.positionals[0]
        if len(args.positionals) > 1:
            output_path = args.positionals[1]

    return input_path, output_path, args.s3_uri, args.pg_query, args.gl_connector


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> None:
    """Entry point."""
    input_path, output_path, s3_uri, pg_query, gl_connector = _parse_args(argv)

    if gl_connector:
        rows = _query_gl_connector(pg_query)  # type: ignore[arg-type]
    else:
        payload = _load_payload(input_path)  # type: ignore[arg-type]
        rows = _extract_rows(payload)

    _write_csv(rows, output_path)

    if s3_uri:
        _upload_to_s3(output_path, s3_uri)

    result: dict[str, Any] = {"csv_path": output_path, "row_count": len(rows)}
    if s3_uri:
        result["s3_uri"] = s3_uri
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main(sys.argv)
