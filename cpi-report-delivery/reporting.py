"""Reporting utility functions for Markdown, HTML, and PDF conversion.

Author:
    Jordan Hakiki Sipahutar (jordan.h.sipahutar@gdplabs.id)
"""

import re
import markdown2
from weasyprint import HTML


def simple_markdown_to_html(markdown_text: str) -> str:
    """Convert markdown to HTML with table support."""
    return markdown2.markdown(markdown_text, extras=["tables", "fenced-code-blocks"])


def _should_use_landscape(markdown_text: str) -> bool:
    """Determine if landscape orientation should be used based on table width."""
    # Find all table header rows (e.g. | col1 | col2 |)
    table_rows = re.findall(r"^\|.*\|$", markdown_text, re.MULTILINE)
    for row in table_rows:
        # Count number of columns by counting '|' and subtracting 1 (ignoring empty ends)
        cols = len([c for c in row.split("|") if c.strip()])
        if cols > 6:  # Threshold for landscape
            return True
    return False


def generate_pdf_from_markdown(markdown_text: str, output_path: str) -> bool:
    """Generate a PDF report from markdown with styling and landscape support."""
    try:
        html_body = simple_markdown_to_html(markdown_text)

        landscape = _should_use_landscape(markdown_text)
        page_size = "landscape" if landscape else "A4"

        html_template = f"""
        <html>
        <head>
            <style>
                @page {{
                    size: {page_size};
                    margin: 1.5cm;
                }}
                body {{
                    font-family: 'Helvetica', 'Arial', sans-serif;
                    line-height: 1.6;
                    color: #333;
                    margin: 0;
                    padding: 0;
                }}
                h1, h2, h3, h4 {{ color: #2c3e50; }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin: 20px 0;
                    font-size: 0.9em;
                    table-layout: auto;
                }}
                th, td {{
                    border: 1px solid #ccc;
                    padding: 10px;
                    text-align: left;
                    word-wrap: break-word;
                }}
                th {{ background-color: #f2f2f2; font-weight: bold; }}
                tr:nth-child(even) {{ background-color: #fafafa; }}
                pre {{
                    background-color: #f8f8f8;
                    padding: 10px;
                    border: 1px solid #ddd;
                    overflow-x: auto;
                }}
                code {{ font-family: 'Courier New', Courier, monospace; }}
            </style>
        </head>
        <body>
            {html_body}
        </body>
        </html>
        """
        HTML(string=html_template).write_pdf(output_path)
        return True
    except Exception as e:
        print(f"Error generating PDF: {e}")
        return False
