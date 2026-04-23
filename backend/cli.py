"""CLI for running extraction without web UI. Essential for rapid iteration."""

import asyncio
import json
import logging
import sys
from pathlib import Path

import click

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.pipeline.classifier import classify_document, classify_by_filename, classify_by_content
from backend.pipeline.parser import parse_document
from backend.pipeline.extractor import extract_document, score_confidence
from backend.models.schemas import ConfidenceLevel

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


@click.group()
def cli():
    """Digital Direction — Telecom extraction CLI."""
    pass


@cli.command()
@click.argument("file_path")
def classify(file_path: str):
    """Classify a single file (carrier, doc type, format, account)."""
    result = asyncio.run(classify_document(file_path))
    click.echo(json.dumps(result.model_dump(), indent=2, default=str))


@cli.command()
@click.argument("input_dir")
def classify_all(input_dir: str):
    """Classify all files in a directory."""
    asyncio.run(_classify_all(input_dir))


async def _classify_all(input_dir: str):
    p = Path(input_dir)
    results = {"classified": 0, "unclassified": 0, "total": 0}

    for f in sorted(p.rglob("*")):
        if f.is_dir() or f.name == ".DS_Store":
            continue
        results["total"] += 1

        r = await classify_document(str(f))

        carrier = r.carrier or "?"
        doc_type = r.document_type or "?"
        variant = r.format_variant or "?"
        account = r.account_number or "?"

        if carrier != "?":
            results["classified"] += 1
        else:
            results["unclassified"] += 1

        click.echo(f"  {carrier:12} {doc_type:15} {variant:35} {account:20} | {f.name[:50]}")

    click.echo(f"\n{results['classified']}/{results['total']} classified "
               f"({results['unclassified']} unclassified)")


@cli.command()
@click.argument("file_path")
@click.option("--carrier", "-c", default=None, help="Override carrier (skip classification)")
@click.option("--doc-type", "-t", default=None, help="Override document type")
@click.option("--output", "-o", default=None, help="Output JSON file path")
def extract(file_path: str, carrier: str, doc_type: str, output: str):
    """Extract 60-field rows from a single file."""
    asyncio.run(_extract(file_path, carrier, doc_type, output))


async def _extract(file_path: str, carrier: str, doc_type: str, output: str):
    # Classify if not overridden
    if not carrier or not doc_type:
        result = await classify_document(file_path)
        carrier = carrier or result.carrier
        doc_type = doc_type or result.document_type
        format_variant = result.format_variant
        logger.info(f"Classified: carrier={carrier}, type={doc_type}, variant={format_variant}")
    else:
        format_variant = None

    if not carrier:
        click.echo("ERROR: Could not determine carrier. Use --carrier to override.")
        return

    # Parse
    parsed = parse_document(file_path, carrier, doc_type, format_variant)
    click.echo(f"Parsed: {len(parsed.sections)} sections")

    # Extract
    rows, responses = await extract_document(parsed)
    click.echo(f"Extracted: {len(rows)} rows")

    # Score confidence
    confidence_scores = score_confidence(rows)

    # Output
    output_data = []
    for i, (row, scores) in enumerate(zip(rows, confidence_scores)):
        row_dict = row.model_dump(exclude_none=True)
        row_dict["_confidence"] = {k: v.value for k, v in scores.items() if v != ConfidenceLevel.MISSING}
        row_dict["_row_index"] = i
        output_data.append(row_dict)

    # Cost summary
    total_input = sum(r.input_tokens for r in responses)
    total_output = sum(r.output_tokens for r in responses)
    total_cost = sum(r.estimated_cost_usd for r in responses)
    click.echo(f"Cost: {total_input} input + {total_output} output tokens = ${total_cost:.4f}")

    if output:
        Path(output).write_text(json.dumps(output_data, indent=2, default=str))
        click.echo(f"Saved to {output}")
    else:
        # Print first 3 rows as preview
        for row_data in output_data[:3]:
            click.echo(json.dumps(row_data, indent=2, default=str))
        if len(output_data) > 3:
            click.echo(f"... ({len(output_data) - 3} more rows)")


@cli.command()
@click.argument("input_dir")
@click.option("--output", "-o", default="extraction_results.json", help="Output JSON file")
def extract_all(input_dir: str, output: str):
    """Extract all files in a directory."""
    asyncio.run(_extract_all(input_dir, output))


async def _extract_all(input_dir: str, output: str):
    p = Path(input_dir)
    all_results = []
    total_cost = 0.0

    for f in sorted(p.rglob("*")):
        if f.is_dir() or f.name == ".DS_Store":
            continue

        click.echo(f"\n{'='*60}")
        click.echo(f"Processing: {f.name}")

        try:
            result = await classify_document(str(f))
            if not result.carrier:
                click.echo(f"  SKIP: could not classify")
                continue

            parsed = parse_document(str(f), result.carrier, result.document_type, result.format_variant)
            rows, responses = await extract_document(parsed)
            cost = sum(r.estimated_cost_usd for r in responses)
            total_cost += cost

            click.echo(f"  {result.carrier} | {result.document_type} | {len(rows)} rows | ${cost:.4f}")

            for row in rows:
                row_dict = row.model_dump(exclude_none=True)
                row_dict["_source_file"] = f.name
                row_dict["_carrier"] = result.carrier
                all_results.append(row_dict)

        except Exception as e:
            click.echo(f"  ERROR: {e}")
            continue

    Path(output).write_text(json.dumps(all_results, indent=2, default=str))
    click.echo(f"\n{'='*60}")
    click.echo(f"Total: {len(all_results)} rows extracted, ${total_cost:.4f} total cost")
    click.echo(f"Saved to {output}")


@cli.command()
@click.argument("input_dir")
@click.option("--client", "-c", default="", help="Client name")
@click.option("--output", "-o", default="pipeline_results.json", help="Output JSON file")
def pipeline(input_dir: str, client: str, output: str):
    """Run full pipeline: classify → parse → extract → merge → validate."""
    asyncio.run(_pipeline(input_dir, client, output))


async def _pipeline(input_dir: str, client: str, output: str):
    from backend.pipeline.orchestrator import process_upload

    click.echo(f"Running full pipeline on {input_dir}")
    result = await process_upload(input_dir, client_name=client)

    # Output merged rows
    output_data = []
    for row in result.merged_rows:
        row_dict = row.model_dump(exclude_none=True)
        output_data.append(row_dict)

    Path(output).write_text(json.dumps(output_data, indent=2, default=str))

    click.echo(f"\n{'='*60}")
    click.echo(f"Files: {result.documents_processed} processed, {result.documents_failed} failed, {result.documents_skipped} skipped")
    click.echo(f"Rows: {result.total_rows} raw → {len(result.merged_rows)} merged")
    click.echo(f"Cost: ${result.total_cost_usd:.4f}")
    click.echo(f"Accounts: {len(result.account_groups)}")
    for key, group in result.account_groups.items():
        docs = ", ".join(f"{dt}({sum(len(dr.rows) for dr in dr_list)})" for dt, dr_list in group.documents.items())
        click.echo(f"  {key}: {docs}")
    click.echo(f"Saved to {output}")


@cli.command("analyze-corrections")
@click.argument("carrier")
@click.option("--min-count", "-n", default=3, help="Minimum correction count to trigger suggestion")
@click.option("--diagnose", is_flag=True, help="Run root-cause diagnosis on undiagnosed corrections")
def analyze_corrections(carrier: str, min_count: int, diagnose: bool):
    """Analyze correction patterns and suggest config updates for a carrier."""
    from backend.services.feedback import analyze_correction_patterns, diagnose_correction

    click.echo(f"Analyzing correction patterns for {carrier} (min_count={min_count})")

    suggestions = analyze_correction_patterns(carrier, min_count=min_count)

    if not suggestions:
        click.echo("No correction patterns found. Need more corrections to generate suggestions.")
        return

    click.echo(f"\n{'='*70}")
    click.echo(f"{'CORRECTION PATTERN ANALYSIS':^70}")
    click.echo(f"{'='*70}")

    for i, s in enumerate(suggestions, 1):
        click.echo(f"\n{i}. [{s.suggestion_type.upper()}] {s.field_name}")
        click.echo(f"   Corrected {s.correction_count} times → '{s.suggested_value}'")
        if s.example_extracted_values:
            click.echo(f"   Wrong values seen: {s.example_extracted_values[:3]}")
        click.echo(f"   {s.explanation}")

    click.echo(f"\n{'='*70}")
    click.echo(f"Total: {len(suggestions)} suggestions")


if __name__ == "__main__":
    cli()
