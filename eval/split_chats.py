"""Deterministic, leak-free stratified splitting of support chat transcripts for T10."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from eval.pii import scrub_pii

LINE_RE = re.compile(r"^#(\d+)\s+\[([^\]]+)\]\s+\[([^\]]*)\]\s*(.*)$")
MASK_TOKEN_RE = re.compile(r"\[(?:email|номер|отправил файл|тел)\]", re.IGNORECASE)
GREETING_RE = re.compile(
    r"\b(здравствуйте|добрый\s+день|доброе\s+утро|добрый\s+вечер|приветствую|"
    r"подскажите|пожалуйста|спасибо|коллеги)\b",
    re.IGNORECASE,
)
NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")

DEFAULT_ARCHIVE = Path("data/raw/чаты поддержки.zip")
DEFAULT_PRIVATE_DIR = Path("data/private")
DEFAULT_SUMMARY_OUT = Path("eval/split_summary.json")
DEFAULT_SEED = 20261126
# Pinned into open_dev:
# - T02/T03 explicit references (#100646, #100625, #103038)
# - Paraphrased examples & typos from docs/data_audit.md
# - Repeat "Эшелон" inquiries in part 2 (part 3 is also pinned)
DEFAULT_PINNED_IDS = (
    100646,
    100625,
    103038,
    102976,
    103016,
    102790,
    101490,
    102802,
    102937,
    101766,
    102972,
    102847,
    102888,
    102635,
    102549,
    103012,
    101730,
    101796,
    101860,
    101838,
    101976,
    102151,
)
DEFAULT_PINNED_FILES = ("buy_part_3_masked.txt",)


def default_holdout_dir() -> Path:
    """Return external holdout directory outside the repository tree."""
    env_dir = os.environ.get("EVAL_HOLDOUT_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path(tempfile.gettempdir()) / "iikuznitsa-eval-private"


def resolve_seed(cli_seed: int | None) -> tuple[int, str]:
    """Resolve split seed from CLI, EVAL_SPLIT_SEED env var, or fallback default."""
    if cli_seed is not None:
        return cli_seed, "cli"
    env_seed = os.environ.get("EVAL_SPLIT_SEED")
    if env_seed is not None and env_seed.strip():
        try:
            return int(env_seed.strip()), "env:EVAL_SPLIT_SEED"
        except ValueError as exc:
            raise ValueError(f"EVAL_SPLIT_SEED must be an integer, got {env_seed!r}") from exc
    return DEFAULT_SEED, "default"


@dataclass(frozen=True, slots=True)
class ChatRecord:
    """Single parsed customer transcript line from the support archive."""

    chat_id: int
    category: str
    page_url: str
    text: str
    source_file: str


def compute_sha256(path: Path) -> str:
    """Return hex SHA-256 digest of a file."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def parse_archive(archive_path: Path) -> tuple[str, list[ChatRecord]]:
    """Load and validate all chat transcripts from the support zip archive."""
    archive_sha256 = compute_sha256(archive_path)
    records: list[ChatRecord] = []
    seen_ids: set[int] = set()
    with zipfile.ZipFile(archive_path) as zf:
        for member in sorted(zf.namelist()):
            if not member.endswith(".txt"):
                continue
            raw_text = zf.read(member).decode("utf-8")
            for line_no, line in enumerate(raw_text.splitlines(), start=1):
                if not line.strip():
                    continue
                match = LINE_RE.match(line)
                if match is None:
                    raise ValueError(f"Malformed line in {member}:{line_no}")
                chat_id_str, category, page_url, text = match.groups()
                chat_id = int(chat_id_str)
                if chat_id in seen_ids:
                    raise ValueError(f"Duplicate chat ID #{chat_id} in {member}:{line_no}")
                seen_ids.add(chat_id)
                records.append(
                    ChatRecord(
                        chat_id=chat_id,
                        category=category.strip(),
                        page_url=page_url.strip(),
                        text=text.strip(),
                        source_file=member,
                    )
                )
    return archive_sha256, records


def normalize_chat_text(text: str) -> str:
    """Normalize chat text for near-duplicate clustering without keeping raw tokens."""
    base = text.casefold().replace("ё", "е")
    base = MASK_TOKEN_RE.sub(" ", base)
    stripped = GREETING_RE.sub(" ", base)
    stripped = NON_WORD_RE.sub(" ", stripped)
    stripped = WHITESPACE_RE.sub(" ", stripped).strip()
    if len(stripped) >= 12:
        return stripped
    fallback = NON_WORD_RE.sub(" ", base)
    return WHITESPACE_RE.sub(" ", fallback).strip()


def char_shingles(text: str, k: int = 4) -> set[str]:
    """Build character k-shingles for Jaccard similarity."""
    if not text:
        return set()
    if len(text) < k:
        return {text}
    return {text[idx : idx + k] for idx in range(len(text) - k + 1)}


def _shingle_jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def cluster_records(
    records: list[ChatRecord],
    *,
    jaccard_threshold: float = 0.55,
    seq_threshold: float = 0.70,
    prefilter_jaccard: float = 0.30,
    shingle_size: int = 4,
) -> list[list[int]]:
    """Cluster record indices into connected near-duplicate components."""
    n = len(records)
    norms = [normalize_chat_text(rec.text) for rec in records]
    shingles = [char_shingles(norm, k=shingle_size) for norm in norms]

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for i in range(n):
        norm_i = norms[i]
        sh_i = shingles[i]
        matcher = SequenceMatcher(None, norm_i, "")
        for j in range(i + 1, n):
            norm_j = norms[j]
            if norm_i and norm_i == norm_j:
                union(i, j)
                continue
            jc = _shingle_jaccard(sh_i, shingles[j])
            if jc >= jaccard_threshold:
                union(i, j)
                continue
            if jc >= prefilter_jaccard:
                matcher.set_seq2(norm_j)
                if (
                    matcher.real_quick_ratio() >= seq_threshold
                    and matcher.quick_ratio() >= seq_threshold
                    and matcher.ratio() >= seq_threshold
                ):
                    union(i, j)

    groups: dict[int, list[int]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)

    clusters = [sorted(indices, key=lambda i: records[i].chat_id) for indices in groups.values()]
    clusters.sort(key=lambda c: records[c[0]].chat_id)
    return clusters


def _build_holdout_case(record: ChatRecord, cluster_id: str) -> dict:
    """Convert a holdout chat record into a PII-scrubbed evaluation case for run_eval.py."""
    case_assertions: dict[str, object] = {
        "forbid_zero_price": True,
        "require_sources_on_answer": True,
        "check_catalog_prices": True,
    }
    if record.category == "Госзакупки / процедуры":
        case_assertions["expect_handoff"] = True
        case_assertions["allowed_handoff_reasons"] = ["tender", "special_terms", "customer_request"]
    return {
        "id": f"holdout-{record.chat_id}",
        "chat_id": record.chat_id,
        "suite": "holdout",
        "sanitize_pii": True,
        "modes": ["mvp", "full"],
        "category": record.category,
        "source_file": record.source_file,
        "page_url": record.page_url,
        "cluster_id": cluster_id,
        "turns": [
            {
                "action": {"type": "message", "text": scrub_pii(record.text)},
                "assertions": {
                    "forbid_zero_price": True,
                    "require_sources_on_answer": True,
                    "check_catalog_prices": True,
                },
            }
        ],
        "assertions": case_assertions,
    }


def split_records(
    records: list[ChatRecord],
    clusters: list[list[int]],
    *,
    archive_filename: str,
    archive_sha256: str,
    holdout_ratio: float = 0.20,
    seed: int = DEFAULT_SEED,
    seed_source: str = "default",
    pinned_ids: tuple[int, ...] = DEFAULT_PINNED_IDS,
    pinned_files: tuple[str, ...] = DEFAULT_PINNED_FILES,
) -> tuple[dict, dict, dict]:
    """Perform stratified cluster-level split into open_dev and private_holdout."""
    if not 0.0 < holdout_ratio < 1.0:
        raise ValueError(f"holdout_ratio must be in (0, 1), got {holdout_ratio}")

    cat_totals = Counter(rec.category for rec in records)
    target_holdout = {
        cat: (max(1, round(total * holdout_ratio)) if total >= 2 else 0) for cat, total in cat_totals.items()
    }

    pinned_id_set = set(pinned_ids)
    pinned_file_set = set(pinned_files)

    # First identify clusters strictly pinned by ID (e.g. #100646 and audit mentions).
    id_pinned_clusters: set[int] = set()
    file_pinned_clusters: set[int] = set()
    for c_idx, indices in enumerate(clusters):
        if any(records[i].chat_id in pinned_id_set for i in indices):
            id_pinned_clusters.add(c_idx)
        if any(records[i].source_file in pinned_file_set for i in indices):
            file_pinned_clusters.add(c_idx)

    # Check if pinning all clusters touching pinned_files still satisfies holdout quotas.
    combined_pinned = id_pinned_clusters | file_pinned_clusters
    unpinned_cat_counts: Counter[str] = Counter()
    for c_idx, indices in enumerate(clusters):
        if c_idx not in combined_pinned:
            for i in indices:
                unpinned_cat_counts[records[i].category] += 1

    use_file_pin = bool(pinned_file_set) and all(unpinned_cat_counts[cat] >= target_holdout[cat] for cat in cat_totals)
    forced_dev_clusters = combined_pinned if use_file_pin else id_pinned_clusters

    unpinned_cluster_indices = [c_idx for c_idx in range(len(clusters)) if c_idx not in forced_dev_clusters]

    def cluster_sort_key(c_idx: int) -> tuple[int, str]:
        indices = clusters[c_idx]
        min_id = records[indices[0]].chat_id
        digest = hashlib.sha256(f"{seed}:{min_id}".encode()).hexdigest()
        return (1 if len(indices) == 1 else 0, digest)

    unpinned_cluster_indices.sort(key=cluster_sort_key)

    holdout_remaining = dict(target_holdout)
    dev_cluster_indices: list[int] = sorted(forced_dev_clusters)
    holdout_cluster_indices: list[int] = []

    for c_idx in unpinned_cluster_indices:
        indices = clusters[c_idx]
        cluster_cats = Counter(records[i].category for i in indices)
        if all(holdout_remaining[cat] >= need for cat, need in cluster_cats.items()):
            holdout_cluster_indices.append(c_idx)
            for cat, need in cluster_cats.items():
                holdout_remaining[cat] -= need
        else:
            dev_cluster_indices.append(c_idx)

    dev_indices = [i for c_idx in dev_cluster_indices for i in clusters[c_idx]]
    holdout_indices = [i for c_idx in holdout_cluster_indices for i in clusters[c_idx]]

    dev_ids = sorted(records[i].chat_id for i in dev_indices)
    holdout_ids = sorted(records[i].chat_id for i in holdout_indices)
    overlap_ids = set(dev_ids) & set(holdout_ids)
    overlap_clusters = set(dev_cluster_indices) & set(holdout_cluster_indices)

    if overlap_ids or overlap_clusters:
        raise RuntimeError(f"Split overlap detected: ids={len(overlap_ids)}, clusters={len(overlap_clusters)}")
    if any(rec.chat_id == 100646 for rec in records) and 100646 not in dev_ids:
        raise RuntimeError("Anchor chat #100646 must belong to open_dev")

    dev_cat_counts = Counter(records[i].category for i in dev_indices)
    holdout_cat_counts = Counter(records[i].category for i in holdout_indices)
    categories_summary = {
        cat: {
            "total": cat_totals[cat],
            "open_dev": dev_cat_counts[cat],
            "private_holdout": holdout_cat_counts[cat],
        }
        for cat in sorted(cat_totals)
    }

    cluster_size_hist = Counter(len(c) for c in clusters)
    anchor_cluster_idx = next(
        (c_idx for c_idx, idxs in enumerate(clusters) if any(records[i].chat_id == 100646 for i in idxs)),
        None,
    )

    dev_payload = {
        "schema_version": "1.0",
        "split": "open_dev",
        "archive_filename": archive_filename,
        "archive_sha256": archive_sha256,
        "seed": seed,
        "count": len(dev_ids),
        "ids": dev_ids,
    }

    holdout_cases = []
    for c_idx in sorted(holdout_cluster_indices):
        indices = clusters[c_idx]
        cluster_id = f"c-{records[indices[0]].chat_id}"
        for i in indices:
            holdout_cases.append(_build_holdout_case(records[i], cluster_id))
    holdout_cases.sort(key=lambda item: item["chat_id"])

    holdout_payload = {
        "schema_version": "1.0",
        "split": "private_holdout",
        "archive_filename": archive_filename,
        "archive_sha256": archive_sha256,
        "seed": seed,
        "count": len(holdout_ids),
        "ids": holdout_ids,
        "category_counts": {cat: holdout_cat_counts[cat] for cat in sorted(cat_totals)},
        "cases": holdout_cases,
    }

    total = len(records)
    seed_sha256_prefix = hashlib.sha256(str(seed).encode()).hexdigest()[:12]
    summary_payload = {
        "schema_version": "1.1",
        "archive_filename": archive_filename,
        "archive_sha256": archive_sha256,
        "seed_source": seed_source,
        "seed_sha256_prefix": seed_sha256_prefix,
        "holdout_ratio_target": holdout_ratio,
        "similarity_rules": {
            "shingle_k": 4,
            "char_shingle_jaccard_threshold": 0.55,
            "sequence_matcher_prefilter_jaccard": 0.30,
            "sequence_matcher_threshold": 0.70,
            "greeting_stripping_min_chars": 12,
        },
        "total_records": total,
        "unique_ids_count": len({r.chat_id for r in records}),
        "open_dev_count": len(dev_ids),
        "private_holdout_count": len(holdout_ids),
        "open_dev_ratio": round(len(dev_ids) / total, 4) if total else 0.0,
        "private_holdout_ratio": round(len(holdout_ids) / total, 4) if total else 0.0,
        "overlap_count": len(overlap_ids),
        "cluster_overlap_count": len(overlap_clusters),
        "pinned_ids_count": len(pinned_id_set & {r.chat_id for r in records}),
        "pinned_files_applied": sorted(pinned_file_set) if use_file_pin else [],
        "anchor_100646_in_open_dev": (100646 in dev_ids) if any(r.chat_id == 100646 for r in records) else None,
        "anchor_100646_cluster_in_open_dev": (
            (anchor_cluster_idx in dev_cluster_indices) if anchor_cluster_idx is not None else None
        ),
        "duplicate_clusters": {
            "total_clusters": len(clusters),
            "multi_item_clusters": sum(1 for c in clusters if len(c) > 1),
            "clustered_records": sum(len(c) for c in clusters if len(c) > 1),
            "size_distribution": {str(size): count for size, count in sorted(cluster_size_hist.items())},
            "open_dev_clusters": len(dev_cluster_indices),
            "private_holdout_clusters": len(holdout_cluster_indices),
        },
        "categories": categories_summary,
    }
    return dev_payload, holdout_payload, summary_payload


def run_split(
    archive_path: Path,
    private_dir: Path,
    summary_out: Path,
    *,
    holdout_dir: Path | None = None,
    holdout_ratio: float = 0.20,
    seed: int | None = None,
) -> tuple[dict, dict, dict]:
    """Execute the full split pipeline and write private, holdout, and public outputs."""
    resolved_seed, seed_source = resolve_seed(seed)
    target_holdout_dir = holdout_dir if holdout_dir is not None else private_dir
    archive_sha256, records = parse_archive(archive_path)
    clusters = cluster_records(records)
    dev_payload, holdout_payload, summary_payload = split_records(
        records,
        clusters,
        archive_filename=archive_path.name,
        archive_sha256=archive_sha256,
        holdout_ratio=holdout_ratio,
        seed=resolved_seed,
        seed_source=seed_source,
    )

    private_dir.mkdir(parents=True, exist_ok=True)
    target_holdout_dir.mkdir(parents=True, exist_ok=True)
    summary_out.parent.mkdir(parents=True, exist_ok=True)

    (private_dir / "dev_ids.json").write_text(
        json.dumps(dev_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (target_holdout_dir / "holdout_eval.json").write_text(
        json.dumps(holdout_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary_out.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dev_payload, holdout_payload, summary_payload


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--private-dir", type=Path, default=DEFAULT_PRIVATE_DIR)
    parser.add_argument(
        "--holdout-dir",
        type=Path,
        default=None,
        help="External directory outside the repo for holdout_eval.json (env: EVAL_HOLDOUT_DIR)",
    )
    parser.add_argument("--summary-out", type=Path, default=DEFAULT_SUMMARY_OUT)
    parser.add_argument("--holdout-ratio", type=float, default=0.20)
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Deterministic split seed (defaults to EVAL_SPLIT_SEED env var, then fallback default)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    holdout_dir = args.holdout_dir if args.holdout_dir is not None else default_holdout_dir()
    _, _, summary = run_split(
        args.archive,
        args.private_dir,
        args.summary_out,
        holdout_dir=holdout_dir,
        holdout_ratio=args.holdout_ratio,
        seed=args.seed,
    )
    print(
        f"Split {summary['total_records']} records -> "
        f"open_dev={summary['open_dev_count']} ({summary['open_dev_ratio']:.1%}), "
        f"private_holdout={summary['private_holdout_count']} ({summary['private_holdout_ratio']:.1%}), "
        f"clusters={summary['duplicate_clusters']['total_clusters']}, "
        f"overlap_count={summary['overlap_count']}, "
        f"seed_source={summary['seed_source']}, "
        f"holdout_path={holdout_dir / 'holdout_eval.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
