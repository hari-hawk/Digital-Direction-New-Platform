"""Semantic-enrichment build for the Platform repo's graphify graph.

Mirrors graphify.watch._rebuild_code but ALSO runs Gemini semantic
extraction over a CURATED allow-list of files (docs/, configs/, root MDs)
so the resulting graph carries cross-doc edges (PENDING priorities → code,
deploy doc → endpoints, carrier YAML → pipeline functions).

CRITICAL: detect() does NOT respect .gitignore. The allow-list below is
what stops 400+ client-billing PDFs in storage/ and data/ from being
sent to Gemini. Don't widen it without thinking.

Reads GEMINI_API_KEY from the environment. Never prints, logs, or
persists the key. Wrap with `scripts/refresh_graph.sh --semantic` so
the platform's .env is sourced automatically.

Run directly:
    GEMINI_API_KEY=... /Users/.../graphifyy/bin/python scripts/graphify_semantic_build.py [PROJECT_ROOT]
"""
import os
import json
import subprocess
import sys
from pathlib import Path

API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
if not API_KEY:
    sys.exit("ERROR: GEMINI_API_KEY env var is not set. Aborting.")

PROJECT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
OUT = PROJECT / "graphify-out"
OUT.mkdir(exist_ok=True)

from graphify.detect import detect, save_manifest
from graphify.extract import extract, _get_extractor
from graphify.llm import extract_corpus_parallel, estimate_cost
from graphify.build import build_from_json
from graphify.cluster import cluster, score_all
from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.report import generate
from graphify.export import to_json, to_html


# ── Allow-list (DO NOT widen without inspecting what gets pulled in) ──
SEM_ALLOWED_PREFIXES = ("docs/", "configs/")
SEM_ALLOWED_ROOT_NAMES = {
    "CLAUDE.md", "PENDING.md", "README.md", "TODOS.md",
    "LANGFUSE_QUICK_START.md", "AGENTS.md",
}


def _is_safe_for_semantic(p: Path) -> bool:
    """Filter that keeps client-data PDFs in storage/ and data/ out of LLM calls."""
    try:
        rel = p.resolve().relative_to(PROJECT)
    except ValueError:
        return False
    rel_str = str(rel)
    if rel_str in SEM_ALLOWED_ROOT_NAMES:
        return True
    return any(rel_str.startswith(prefix) for prefix in SEM_ALLOWED_PREFIXES)


def _git_head(cwd: Path) -> "str | None":
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd,
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def main() -> None:
    print(f"[1/7] detecting files in {PROJECT}…")
    detected = detect(PROJECT)
    totals = detected.get("total_files", 0)
    print(f"      {totals} files, ~{detected.get('total_words', 0)} words")
    for bucket, items in detected.get("files", {}).items():
        if items:
            print(f"        {bucket:10s} {len(items)}")

    # ── AST extraction over code (free) ──
    print("[2/7] AST extraction (tree-sitter, free)…")
    code_files = [Path(f) for f in detected["files"].get("code", [])]
    for doc_file in detected["files"].get("document", []):
        p = Path(doc_file)
        if _get_extractor(p) is not None:
            code_files.append(p)
    ast_result = (
        extract(code_files, cache_root=PROJECT)
        if code_files
        else {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}
    )
    print(f"      AST: {len(ast_result['nodes'])} nodes, {len(ast_result['edges'])} edges")

    # ── Semantic extraction (Gemini) — allow-listed only ──
    sem_files: list[Path] = []
    skipped_unsafe = 0
    for bucket in ("document", "paper", "image"):
        for f in detected["files"].get(bucket, []):
            p = Path(f)
            if _get_extractor(p) is not None:
                continue  # AST handled it
            if not _is_safe_for_semantic(p):
                skipped_unsafe += 1
                continue
            sem_files.append(p)
    print(
        f"[3/7] semantic extraction over {len(sem_files)} non-code files via Gemini "
        f"(skipped {skipped_unsafe} client-data files in storage/data/evals)…"
    )
    sem_result = (
        extract_corpus_parallel(sem_files, backend="gemini", api_key=API_KEY, root=PROJECT)
        if sem_files
        else {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0}
    )
    in_tok = sem_result.get("input_tokens", 0)
    out_tok = sem_result.get("output_tokens", 0)
    cost = estimate_cost("gemini", in_tok, out_tok)
    print(
        f"      Sem: {len(sem_result['nodes'])} nodes, {len(sem_result['edges'])} edges "
        f"({in_tok} in / {out_tok} out tokens, ~${cost:.4f})"
    )

    merged = {
        "nodes": ast_result["nodes"] + sem_result["nodes"],
        "edges": ast_result["edges"] + sem_result["edges"],
        "hyperedges": sem_result.get("hyperedges", []),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
    }
    print(
        f"[4/7] merged: {len(merged['nodes'])} nodes, {len(merged['edges'])} edges, "
        f"{len(merged['hyperedges'])} hyperedges"
    )

    print("[5/7] building NetworkX graph + Leiden clustering…")
    G = build_from_json(merged)
    communities = cluster(G)
    cohesion = score_all(G, communities)
    gods = god_nodes(G)
    surprises = surprising_connections(G, communities)

    labels_file = OUT / ".graphify_labels.json"
    prior_labels: dict[int, str] = {}
    if labels_file.exists():
        try:
            prior_labels = {int(k): v for k, v in json.loads(labels_file.read_text()).items()}
        except Exception:
            pass
    labels = {cid: prior_labels.get(cid, f"Community {cid}") for cid in communities}
    questions = suggest_questions(G, communities, labels)

    print("[6/7] writing graph.json + manifest…")
    commit = _git_head(PROJECT)
    ok = to_json(G, communities, str(OUT / "graph.json"), force=True, built_at_commit=commit)
    if not ok:
        sys.exit("graph.json write rejected (node-count safety check)")
    try:
        save_manifest(detected["files"])
    except Exception as e:
        print(f"      manifest skipped: {e}")

    print("[7/7] writing GRAPH_REPORT.md + graph.html…")
    detection_summary = {
        "files": {k: [str(p) for p in v] for k, v in detected.get("files", {}).items()},
        "total_files": totals,
        "total_words": detected.get("total_words", 0),
    }
    report = generate(
        G, communities, cohesion, labels, gods, surprises, detection_summary,
        {"input": in_tok, "output": out_tok},
        PROJECT.name, suggested_questions=questions, built_at_commit=commit,
    )
    (OUT / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
    try:
        to_html(G, communities, str(OUT / "graph.html"), community_labels=labels or None)
    except ValueError as e:
        print(f"      graph.html skipped: {e}")

    (OUT / ".graphify_root").write_text(str(PROJECT), encoding="utf-8")
    labels_file.write_text(json.dumps({str(k): v for k, v in labels.items()}, indent=2))

    print()
    print(
        f"DONE. {G.number_of_nodes()} nodes · {G.number_of_edges()} edges · "
        f"{len(communities)} communities · ~${cost:.4f} spent"
    )


if __name__ == "__main__":
    main()
