r"""Generate a PDF Padma report, upload it to S3, and send email via GL Connectors.

This script is invoked by ``padma_reporting_agent`` via the ``execute``
filesystem tool. It reads a pre-generated markdown report file (the 10-section
Monthly Customer Review Report) and:

  1. Generates a PDF from the markdown using the bundled ``reporting`` module
     (WeasyPrint under the hood — requires Pango/Cairo at runtime).
  2. Optionally uploads the PDF to S3 using boto3 (streams via multipart for
     large files, no full-file RAM load).
  3. Authenticates with GL Connectors and sends an email with the PDF
     attached, using urllib with multipart/form-data encoding (no requests).

Forked from ``skills/cpi-report-delivery/deliver.py``. Differences:
  - The CSV attachment is gone — Padma PoC does not produce a parallel CSV.
  - Default PDF filename is ``report_padma.pdf``.
  - Default S3 bucket/prefix env vars are ``PADMA_S3_BUCKET`` /
    ``PADMA_S3_PREFIX`` (no shared env names with CPI).

All credentials are read from environment variables injected into the agent
sandbox. Runtime requirements: ``weasyprint`` (Python) and the Pango/Cairo
system libraries.
On Debian/Ubuntu: ``apt install libpango-1.0-0 libpangoft2-1.0-0``.
On macOS: ``brew install pango`` and export
``DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`` before invoking.

Usage:
    python deliver.py \\
        --markdown-file /workspace/tmp/report.md \\
        --email-to "user@example.com,other@gdplabs.id" \\
        --email-subject "Laporan Bulanan Customer Review Padma - April 2026" \\
        [--email-body-file /workspace/tmp/email_body.md] \\
        [--s3-bucket glair-padma-staging] \\
        [--s3-prefix padma_report_base] \\
        [--skip-s3] \\
        [--skip-email]

Environment variables (injected by agent sandbox_env):
    AWS_DEFAULT_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
    AWS_SESSION_TOKEN (optional — for STS temporary credentials)
    AWS_EXPECTED_BUCKET_OWNER (optional)
    GL_CONNECTORS_BASE_URL, GL_CONNECTORS_API_KEY
    GL_CONNECTORS_USERNAME, GL_CONNECTORS_USER_SECRET
    PADMA_S3_BUCKET, PADMA_S3_PREFIX (fallbacks when --s3-bucket/--s3-prefix not given)

On success prints a single JSON line to stdout:
    {"pdf_path": "...", "s3_uris": [...], "email_sent": true}

On failure exits non-zero with a JSON error on stderr.

Authors:
    Ramadha S. Ranuh (ramadha.s.ranuh@gdplabs.id)

References:
    https://gdplabs.gitbook.io/sdk/gl-ai-agent-package/guides/skills
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


def _install_if_missing() -> None:
    """Pip-install packages required at runtime that the sandbox may not have."""
    import importlib.util
    import subprocess

    required = [
        ("weasyprint", "weasyprint"),
        ("markdown2", "markdown2"),
        ("boto3", "boto3"),
        ("botocore", "boto3"),
    ]
    missing = sorted({pkg for mod, pkg in required if importlib.util.find_spec(mod) is None})
    if missing:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])


_install_if_missing()

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import BotoCoreError, ClientError

JAKARTA_TZ = ZoneInfo("Asia/Jakarta")
HTTP_OK = 200


def _fail(message: str, exit_code: int = 1) -> None:
    """Print a JSON error to stderr and exit."""
    sys.stderr.write(json.dumps({"error": message}) + "\n")
    sys.exit(exit_code)


def _multipart_form_data(
    fields: list[tuple[str, str]],
    files: list[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    """Encode fields and files as multipart/form-data (no requests library).

    Args:
        fields: Text fields as ``(name, value)`` pairs.
        files: Binary files as ``(field_name, filename, content_bytes, content_type)`` tuples.

    Returns:
        tuple[bytes, str]: ``(encoded_body, Content-Type header value)`` where the
            Content-Type includes the boundary parameter.
    """
    boundary = "----FormBoundary" + hashlib.sha256(os.urandom(16)).hexdigest()[:24]
    parts: list[bytes] = []

    for name, value in fields:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode()
            + value.encode("utf-8")
            + b"\r\n"
        )

    for field_name, filename, content, content_type in files:
        parts.append(
            (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{field_name}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
            + content
            + b"\r\n"
        )

    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _parse_flag_token(token: str, it: Any, args: dict[str, Any]) -> bool:
    """Dispatch a single CLI flag token into the args dict.

    Args:
        token: The current CLI flag string (e.g. ``--markdown-file``).
        it: Iterator over remaining argv tokens; consumed for value flags.
        args: Mutable configuration dict to update in place.

    Returns:
        bool: True if the token was recognised, False otherwise.
    """
    value_flags: dict[str, str] = {
        "--markdown-file": "markdown_file",
        "--email-body-file": "email_body_file",
        "--email-subject": "email_subject",
        "--s3-bucket": "s3_bucket",
        "--s3-prefix": "s3_prefix",
        "--append-markdown": "append_markdown",
    }
    bool_flags: dict[str, str] = {
        "--skip-s3": "skip_s3",
        "--skip-email": "skip_email",
    }

    if token in value_flags:
        args[value_flags[token]] = next(it)
        return True
    if token in bool_flags:
        args[bool_flags[token]] = True
        return True
    if token == "--email-to":
        args["email_to"] = [e.strip() for e in next(it).split(",") if e.strip()]
        return True
    return False


def _parse_args(argv: list[str]) -> dict[str, Any]:
    """Parse CLI arguments into a configuration dict.

    Args:
        argv: Raw sys.argv list.

    Returns:
        dict[str, Any]: Parsed configuration with keys: markdown_file,
            email_body_file, email_to, email_subject, s3_bucket, s3_prefix,
            skip_s3, skip_email, append_markdown.
    """
    args: dict[str, Any] = {
        "markdown_file": None,
        "email_body_file": None,
        "email_to": [],
        "email_subject": "",
        "s3_bucket": os.environ.get("PADMA_S3_BUCKET", ""),
        "s3_prefix": os.environ.get("PADMA_S3_PREFIX", "padma_report_base"),
        "skip_s3": False,
        "skip_email": False,
        "append_markdown": None,
    }

    it = iter(argv[1:])
    for token in it:
        if token in {"-h", "--help"}:
            sys.stderr.write(__doc__ or "")
            sys.exit(2)
        elif not _parse_flag_token(token, it, args):
            _fail(f"Unknown argument: {token!r}")

    if args.get("append_markdown") is not None:
        if not args["markdown_file"]:
            _fail("--markdown-file is required when using --append-markdown.")
    else:
        if not args["markdown_file"]:
            _fail("--markdown-file is required.")

    return args


def _generate_pdf(markdown_file: str, output_dir: str) -> str:
    """Generate a PDF from a markdown file using the bundled reporting module.

    Args:
        markdown_file: Absolute path to the input markdown file.
        output_dir: Directory to write the output PDF.

    Returns:
        str: Absolute path to the generated PDF file.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from reporting import generate_pdf_from_markdown  # type: ignore[import]  # noqa: PLC0415
    except ImportError as exc:
        _fail(f"Could not import bundled reporting module: {exc}")

    try:
        with open(markdown_file, "r", encoding="utf-8") as fh:
            markdown_content = fh.read()
    except OSError as exc:
        _fail(f"Could not read markdown file {markdown_file!r}: {exc}")

    os.makedirs(output_dir, exist_ok=True)
    pdf_path = os.path.join(output_dir, "report_padma.pdf")

    if not generate_pdf_from_markdown(markdown_content, pdf_path):
        _fail("PDF generation failed — verify markdown_content is non-empty and valid.")

    return pdf_path


def _make_s3_client(region: str) -> Any:
    """Create a boto3 S3 client from environment credentials.

    Args:
        region: AWS region string (e.g. ``ap-southeast-3``).

    Returns:
        botocore.client.S3: Configured S3 client.
    """
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or None,
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY") or None,
        aws_session_token=os.environ.get("AWS_SESSION_TOKEN") or None,
        config=BotocoreConfig(
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _upload_to_s3(file_paths: list[str], s3_bucket: str, s3_prefix: str) -> list[str]:
    """Upload local files to S3 via boto3 and return their s3:// URIs.

    Args:
        file_paths: Absolute local paths of files to upload.
        s3_bucket: Target S3 bucket name.
        s3_prefix: S3 key prefix (timestamp folder appended automatically).

    Returns:
        list[str]: s3:// URIs for successfully uploaded files.
    """
    if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
        sys.stderr.write(
            json.dumps({"warning": "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY not set, skipping S3 upload."}) + "\n"
        )
        return []

    region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION", "ap-southeast-3")
    expected_bucket_owner = os.environ.get("AWS_EXPECTED_BUCKET_OWNER", "")
    s3 = _make_s3_client(region)

    timestamp = datetime.now(JAKARTA_TZ).strftime("%Y%m%d_%H%M%S")
    prefix = s3_prefix.rstrip("/")
    s3_uris: list[str] = []

    for path in file_paths:
        if not os.path.isfile(path):
            sys.stderr.write(json.dumps({"warning": f"File not found, skipping upload: {path}"}) + "\n")
            continue
        filename = os.path.basename(path)
        key = f"{prefix}/{timestamp}/{filename}"
        extra_args: dict[str, str] = {}
        if expected_bucket_owner:
            extra_args["ExpectedBucketOwner"] = expected_bucket_owner
        try:
            s3.upload_file(path, s3_bucket, key, ExtraArgs=extra_args or None)
            s3_uris.append(f"s3://{s3_bucket}/{key}")
        except (BotoCoreError, ClientError) as exc:
            msg = f"S3 upload of {path!r} to s3://{s3_bucket}/{key} failed: {exc}"
            sys.stderr.write(json.dumps({"warning": msg}) + "\n")

    return s3_uris


def _authenticate_gl(base_url: str, api_key: str, username: str, user_secret: str) -> str:
    """Authenticate with GL Connectors and return a bearer token.

    Args:
        base_url: GL Connectors base URL (e.g. https://connector.gdplabs.id).
        api_key: API key sent in X-API-Key header.
        username: GL Connectors user identifier.
        user_secret: GL Connectors user secret (password).

    Returns:
        str: Bearer token for subsequent API calls.
    """
    url = f"{base_url.rstrip('/')}/auth/tokens"
    payload = json.dumps({"identifier": username, "secret": user_secret}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data["data"]["token"]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        _fail(f"GL Connectors auth failed ({exc.code}): {body[:400]}")
    except Exception as exc:
        _fail(f"GL Connectors auth error: {exc}")
    return ""


SEND_EMAIL_PATH = "/connectors/google_mail/send_email"


def _send_email(  # noqa: PLR0913
    send_email_url: str,
    api_key: str,
    token: str,
    recipients: list[str],
    subject: str,
    body_markdown_file: str,
    attachment_paths: list[str],
) -> None:
    """Send an email with attachments via GL Connectors multipart POST.

    Args:
        send_email_url: Fully-qualified send_email endpoint URL.
        api_key: GL Connectors API key (X-API-Key header).
        token: Bearer token from _authenticate_gl.
        recipients: List of recipient email addresses.
        subject: Email subject line.
        body_markdown_file: Path to the markdown file used as the HTML email body.
        attachment_paths: Absolute local paths of files to attach.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from reporting import simple_markdown_to_html  # type: ignore[import]  # noqa: PLC0415
    except ImportError as exc:
        _fail(f"Could not import simple_markdown_to_html from bundled reporting module: {exc}")

    try:
        with open(body_markdown_file, "r", encoding="utf-8") as fh:
            markdown_content = fh.read()
    except OSError as exc:
        _fail(f"Could not read email body file {body_markdown_file!r}: {exc}")

    html_body = simple_markdown_to_html(markdown_content)

    fields: list[tuple[str, str]] = [("mail_to", r) for r in recipients]
    fields.append(("mail_subject", subject))
    fields.append(("mail_body", html_body))

    file_parts: list[tuple[str, str, bytes, str]] = []
    for path in attachment_paths:
        if not os.path.isfile(path):
            sys.stderr.write(json.dumps({"warning": f"Attachment not found, skipping: {path}"}) + "\n")
            continue
        filename = os.path.basename(path)
        content_type, _ = mimetypes.guess_type(filename)
        with open(path, "rb") as fh:
            content = fh.read()
        file_parts.append(("mail_attachments", filename, content, content_type or "application/octet-stream"))

    body, content_type_hdr = _multipart_form_data(fields, file_parts)

    req = urllib.request.Request(
        send_email_url,
        data=body,
        headers={
            "X-API-Key": api_key,
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type_hdr,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status != HTTP_OK:
                raise OSError(f"Email send returned status {resp.status}.")
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        _fail(f"Email send failed ({exc.code}): {error_text[:400]}")
    except Exception as exc:
        _fail(f"Email send error: {exc}")


def _handle_s3_upload(args: dict[str, Any], pdf_path: str) -> list[str]:
    """Upload the PDF to S3 if configured."""
    if args["skip_s3"] or not args["s3_bucket"]:
        return []

    try:
        return _upload_to_s3(
            [pdf_path],
            args["s3_bucket"],
            args["s3_prefix"],
        )
    except Exception as exc:
        sys.stderr.write(json.dumps({"warning": f"S3 upload step failed, continuing to email: {exc}"}) + "\n")
        return []


def _handle_email(args: dict[str, Any], pdf_path: str, output_dir: str) -> bool:
    """Send an email with the PDF attachment."""
    if args["skip_email"] or not args["email_to"]:
        return False

    base_url = os.environ.get("GL_CONNECTORS_BASE_URL", "")
    api_key = os.environ.get("GL_CONNECTORS_API_KEY", "")
    username = os.environ.get("GL_CONNECTORS_USERNAME", "")
    user_secret = os.environ.get("GL_CONNECTORS_USER_SECRET", "")

    missing = [
        name
        for name, val in [
            ("GL_CONNECTORS_BASE_URL", base_url),
            ("GL_CONNECTORS_API_KEY", api_key),
            ("GL_CONNECTORS_USERNAME", username),
            ("GL_CONNECTORS_USER_SECRET", user_secret),
        ]
        if not val
    ]
    if missing:
        _fail(f"Missing GL Connectors env vars: {', '.join(missing)}")

    token = _authenticate_gl(base_url, api_key, username, user_secret)
    send_email_url = f"{base_url.rstrip('/')}{SEND_EMAIL_PATH}"

    body_file = args["email_body_file"]
    if not body_file:
        import re

        with open(args["markdown_file"], "r", encoding="utf-8") as f:
            md_text = f.read()
        match = re.split(r"\n##\s+2\.", md_text, maxsplit=1)
        email_md = match[0] if match else md_text

        body_file = os.path.join(output_dir, "email_body.md")
        with open(body_file, "w", encoding="utf-8") as f:
            f.write(email_md)

    _send_email(
        send_email_url,
        api_key,
        token,
        args["email_to"],
        args["email_subject"],
        body_file,
        [pdf_path],
    )
    return True


def main(argv: list[str]) -> None:
    """Entry point for the deliver script."""
    args = _parse_args(argv)

    if args.get("append_markdown") is not None:
        try:
            with open(args["markdown_file"], "a", encoding="utf-8") as fh:
                fh.write(args["append_markdown"] + "\n")
            sys.stdout.write(
                json.dumps({"success": True, "action": "append_markdown", "file": args["markdown_file"]}) + "\n"
            )
            return
        except OSError as exc:
            _fail(f"Failed to append to markdown file {args['markdown_file']!r}: {exc}")

    if not os.path.isfile(args["markdown_file"]) or os.path.getsize(args["markdown_file"]) == 0:
        _fail(
            f"Validation failed: markdown file {args['markdown_file']!r} is missing or empty. "
            "Please generate the report first."
        )

    output_dir = tempfile.mkdtemp(prefix="padma_report_")
    pdf_path = _generate_pdf(args["markdown_file"], output_dir)

    s3_uris = _handle_s3_upload(args, pdf_path)
    email_sent = _handle_email(args, pdf_path, output_dir)

    result: dict[str, Any] = {
        "pdf_path": pdf_path,
        "s3_uris": s3_uris,
        "email_sent": email_sent,
    }
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main(sys.argv)
