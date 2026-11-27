"""Deterministic, leak-free stratified splitting of support chat transcripts for T10."""

from __future__ import annotations

import argparse
import hashlib
import hmac
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
DEFAULT_SUMMARY_OUT = Path("eval/split_summary.json")
REPO_ROOT = Path(__file__).resolve().parents[1]
HOLDOUT_DIR_NAME = "iikuznitsa-eval-private"
SEED_ENV = "EVAL_SPLIT_SEED"
MIN_SEED_HEX_CHARS = 32  # 128 bits, e.g. python -c "import secrets; print(secrets.token_hex(16))"
SEED_RE = re.compile(rf"[0-9a-fA-F]{{{MIN_SEED_HEX_CHARS},}}")
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
    """External holdout directory: EVAL_HOLDOUT_DIR or $XDG_DATA_HOME (~/.local/share)/iikuznitsa-eval-private."""
    env_dir = os.environ.get("EVAL_HOLDOUT_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home).expanduser() / HOLDOUT_DIR_NAME


def check_holdout_dir(path: Path) -> Path:
    """Refuse holdout locations inside the repository or inside the shared, auto-cleaned temp directory."""
    resolved = path.expanduser().resolve()
    if resolved.is_relative_to(REPO_ROOT.resolve()):
        raise ValueError(f"Holdout directory must be outside the repository: {resolved}")
    for tmp_root in {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve(), Path("/var/tmp").resolve()}:
        if resolved.is_relative_to(tmp_root):
            raise ValueError(f"Holdout directory must not live in a temp directory ({tmp_root}): {resolved}")
    return resolved


def resolve_seed() -> str:
    """Read the secret split seed strictly from EVAL_SPLIT_SEED (>= 128 bits as hex); there is no default."""
    env_seed = (os.environ.get(SEED_ENV) or "").strip()
    if not env_seed:
        raise ValueError(
            f"{SEED_ENV} is not set. Generate a secret 128-bit seed, e.g. "
            "`export EVAL_SPLIT_SEED=\"$(python3 -c 'import secrets; print(secrets.token_hex(16))')\"`, "
            "and keep it private."
        )
    if not SEED_RE.fullmatch(env_seed):
        raise ValueError(f"{SEED_ENV} must be at least {MIN_SEED_HEX_CHARS} hex characters (128 bits)")
    return env_seed.lower()


def _write_private(path: Path, payload: dict) -> None:
    """Write JSON readable only by the owner (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.chmod(path, 0o600)


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
    seed: str,
    holdout_ratio: float = 0.20,
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
        digest = hmac.new(seed.encode(), str(min_id).encode(), hashlib.sha256).hexdigest()
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
        "count": len(holdout_ids),
        "ids": holdout_ids,
        "category_counts": {cat: holdout_cat_counts[cat] for cat in sorted(cat_totals)},
        "cases": holdout_cases,
    }

    total = len(records)
    summary_payload = {
        "schema_version": "1.2",
        "archive_filename": archive_filename,
        "archive_sha256": archive_sha256,
        "seed_policy": "secret EVAL_SPLIT_SEED (>= 128 bit), HMAC-SHA256 cluster order; seed is never published",
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
    holdout_dir: Path,
    summary_out: Path,
    *,
    seed: str,
    holdout_ratio: float = 0.20,
) -> tuple[dict, dict, dict]:
    """Execute the split and write dev_ids.json + holdout_eval.json (0600, dir 0700) and the public summary."""
    payloads = compute_split(archive_path, seed=seed, holdout_ratio=holdout_ratio)
    write_split(holdout_dir, summary_out, payloads)
    return payloads


def run_split_from_manifest(
    archive_path: Path, manifest_path: Path, holdout_dir: Path, summary_out: Path
) -> tuple[dict, dict, dict]:
    """Same outputs and file permissions as run_split(), but dev/holdout come strictly from manifest.json."""
    payloads = compute_split(archive_path, assignment=load_manifest(manifest_path))
    write_split(holdout_dir, summary_out, payloads)
    return payloads


# ---------------------------------------------------------------------------
# Fixed team split (T02 manifest.json): --manifest and --verify-manifest
# ---------------------------------------------------------------------------

MANIFEST_WRAPPER_KEYS = ("items", "records", "rows", "chats")
MANIFEST_ID_KEYS = ("id", "chat_id", "conversation_id")
MANIFEST_SPLIT_VALUES = {
    "dev": "dev",
    "open_dev": "dev",
    "open": "dev",
    "holdout": "holdout",
    "private_holdout": "holdout",
    "private": "holdout",
}


class ManifestError(ValueError):
    """manifest.json cannot be read unambiguously. Messages name entry positions, never chat IDs."""


def _manifest_chat_id(value: object, pos: int) -> int:
    if isinstance(value, bool):
        raise ManifestError(f"manifest entry #{pos}: id must be an integer or a digit string")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"#?\d+", value.strip()):
        return int(value.strip().lstrip("#"))
    raise ManifestError(f"manifest entry #{pos}: id must be an integer or a digit string")


def load_manifest(path: Path) -> dict[int, str]:
    """Read a T02 manifest.json into {chat_id: "dev" | "holdout"}.

    Accepted: a flat list of entries or {"items"|"records"|"rows"|"chats": [...]}; each entry has one of
    id / chat_id / conversation_id and a split (dev/open_dev/open or holdout/private_holdout/private).
    Duplicates, missing fields, unknown split values and unknown layouts are errors.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest {path.name}: {exc.__class__.__name__}") from exc
    if isinstance(data, dict):
        keys = [key for key in MANIFEST_WRAPPER_KEYS if key in data]
        if len(keys) != 1 or not isinstance(data[keys[0]], list):
            raise ManifestError(
                f"unknown manifest layout: expected a list or exactly one of {list(MANIFEST_WRAPPER_KEYS)} with a list"
            )
        entries = data[keys[0]]
    elif isinstance(data, list):
        entries = data
    else:
        raise ManifestError("unknown manifest layout: top level must be a list or an object")
    if not entries:
        raise ManifestError("manifest has no entries")

    assignment: dict[int, str] = {}
    for pos, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ManifestError(f"manifest entry #{pos}: expected an object")
        id_keys = [key for key in MANIFEST_ID_KEYS if key in entry]
        if len(id_keys) != 1:
            raise ManifestError(f"manifest entry #{pos}: expected exactly one of {list(MANIFEST_ID_KEYS)}")
        chat_id = _manifest_chat_id(entry[id_keys[0]], pos)
        split = MANIFEST_SPLIT_VALUES.get(str(entry.get("split", "")).strip().lower())
        if split is None:
            raise ManifestError(f"manifest entry #{pos}: split must be one of {sorted(MANIFEST_SPLIT_VALUES)}")
        if chat_id in assignment:
            raise ManifestError(f"manifest entry #{pos}: duplicate id")
        assignment[chat_id] = split
    return assignment


def split_records_from_manifest(
    records: list[ChatRecord],
    clusters: list[list[int]],
    assignment: dict[int, str],
    *,
    archive_filename: str,
    archive_sha256: str,
) -> tuple[dict, dict, dict]:
    """Put every chat into dev/holdout strictly by the manifest (no HMAC order, no seed)."""
    archive_ids = {rec.chat_id for rec in records}
    missing = len(archive_ids - assignment.keys())
    unknown = len(assignment.keys() - archive_ids)
    if missing or unknown:
        raise ManifestError(
            f"manifest does not cover the archive: {missing} archive chat(s) missing from manifest, "
            f"{unknown} manifest id(s) not in archive"
        )

    cluster_of = {i: c_idx for c_idx, indices in enumerate(clusters) for i in indices}
    dev_indices = [i for i, rec in enumerate(records) if assignment[rec.chat_id] == "dev"]
    holdout_indices = [i for i, rec in enumerate(records) if assignment[rec.chat_id] == "holdout"]
    dev_ids = sorted(records[i].chat_id for i in dev_indices)
    holdout_ids = sorted(records[i].chat_id for i in holdout_indices)
    dev_clusters = {cluster_of[i] for i in dev_indices}
    holdout_clusters = {cluster_of[i] for i in holdout_indices}
    # The manifest is authoritative; near-duplicate clusters split across dev/holdout are reported, not moved.
    overlap_clusters = dev_clusters & holdout_clusters

    cat_totals = Counter(rec.category for rec in records)
    dev_cat_counts = Counter(records[i].category for i in dev_indices)
    holdout_cat_counts = Counter(records[i].category for i in holdout_indices)

    dev_payload = {
        "schema_version": "1.0",
        "split": "open_dev",
        "split_source": "manifest",
        "archive_filename": archive_filename,
        "archive_sha256": archive_sha256,
        "count": len(dev_ids),
        "ids": dev_ids,
    }
    holdout_cases = [
        _build_holdout_case(records[i], f"c-{records[clusters[cluster_of[i]][0]].chat_id}") for i in holdout_indices
    ]
    holdout_cases.sort(key=lambda item: item["chat_id"])
    holdout_payload = {
        "schema_version": "1.0",
        "split": "private_holdout",
        "split_source": "manifest",
        "archive_filename": archive_filename,
        "archive_sha256": archive_sha256,
        "count": len(holdout_ids),
        "ids": holdout_ids,
        "category_counts": {cat: holdout_cat_counts[cat] for cat in sorted(cat_totals)},
        "cases": holdout_cases,
    }
    total = len(records)
    has_anchor = 100646 in archive_ids
    summary_payload = {
        "schema_version": "1.2",
        "archive_filename": archive_filename,
        "archive_sha256": archive_sha256,
        "split_source": "manifest",
        "seed_policy": "fixed team manifest.json (T02); no seed is used",
        "total_records": total,
        "unique_ids_count": len(archive_ids),
        "open_dev_count": len(dev_ids),
        "private_holdout_count": len(holdout_ids),
        "open_dev_ratio": round(len(dev_ids) / total, 4) if total else 0.0,
        "private_holdout_ratio": round(len(holdout_ids) / total, 4) if total else 0.0,
        "overlap_count": len(set(dev_ids) & set(holdout_ids)),
        "cluster_overlap_count": len(overlap_clusters),
        "anchor_100646_in_open_dev": (100646 in dev_ids) if has_anchor else None,
        "duplicate_clusters": {
            "total_clusters": len(clusters),
            "multi_item_clusters": sum(1 for c in clusters if len(c) > 1),
            "clustered_records": sum(len(c) for c in clusters if len(c) > 1),
            "open_dev_clusters": len(dev_clusters - holdout_clusters),
            "private_holdout_clusters": len(holdout_clusters - dev_clusters),
        },
        "categories": {
            cat: {"total": cat_totals[cat], "open_dev": dev_cat_counts[cat], "private_holdout": holdout_cat_counts[cat]}
            for cat in sorted(cat_totals)
        },
    }
    return dev_payload, holdout_payload, summary_payload


def _ids_sha256(ids: set[int]) -> str:
    """SHA-256 of the sorted ID set, one decimal ID per line (compare sets without printing them)."""
    return hashlib.sha256("\n".join(str(i) for i in sorted(ids)).encode("ascii")).hexdigest()


def verify_against_manifest(dev_ids: set[int], holdout_ids: set[int], assignment: dict[int, str]) -> dict:
    """Compare computed dev/holdout sets with the manifest. The report holds counts and hashes, never IDs."""
    manifest_dev = {cid for cid, split in assignment.items() if split == "dev"}
    manifest_holdout = {cid for cid, split in assignment.items() if split == "holdout"}
    computed = {cid: "dev" for cid in dev_ids} | {cid: "holdout" for cid in holdout_ids}
    all_ids = computed.keys() | assignment.keys()
    mismatches = sum(1 for cid in all_ids if computed.get(cid) != assignment.get(cid))
    return {
        "exact_match": dev_ids == manifest_dev and holdout_ids == manifest_holdout,
        "computed": {"dev": len(dev_ids), "holdout": len(holdout_ids)},
        "manifest": {"dev": len(manifest_dev), "holdout": len(manifest_holdout)},
        "mismatch_count": mismatches,
        "sha256": {
            "computed_dev": _ids_sha256(dev_ids),
            "computed_holdout": _ids_sha256(holdout_ids),
            "manifest_dev": _ids_sha256(manifest_dev),
            "manifest_holdout": _ids_sha256(manifest_holdout),
        },
    }


def compute_split(
    archive_path: Path,
    *,
    seed: str | None = None,
    assignment: dict[int, str] | None = None,
    holdout_ratio: float = 0.20,
) -> tuple[dict, dict, dict]:
    """Build the split in memory: by manifest assignment when given, otherwise by HMAC order with the seed."""
    archive_sha256, records = parse_archive(archive_path)
    clusters = cluster_records(records)
    if assignment is not None:
        return split_records_from_manifest(
            records, clusters, assignment, archive_filename=archive_path.name, archive_sha256=archive_sha256
        )
    if seed is None:
        raise ValueError("seed is required when no manifest is given")
    return split_records(
        records,
        clusters,
        archive_filename=archive_path.name,
        archive_sha256=archive_sha256,
        seed=seed,
        holdout_ratio=holdout_ratio,
    )


def write_split(holdout_dir: Path, summary_out: Path, payloads: tuple[dict, dict, dict]) -> None:
    """Write dev_ids.json + holdout_eval.json (0600, dir 0700) and the public summary."""
    dev_payload, holdout_payload, summary_payload = payloads
    holdout_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(holdout_dir, 0o700)
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    # dev_ids.json is the complement of the holdout, so it is exactly as sensitive and lives next to it.
    _write_private(holdout_dir / "dev_ids.json", dev_payload)
    _write_private(holdout_dir / "holdout_eval.json", holdout_payload)
    summary_out.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"The seed is read only from {SEED_ENV} (>= {MIN_SEED_HEX_CHARS} hex chars).",
    )
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument(
        "--holdout-dir",
        type=Path,
        default=None,
        help=(
            "Private directory outside the repo and /tmp "
            f"(env: EVAL_HOLDOUT_DIR, default ~/.local/share/{HOLDOUT_DIR_NAME})"
        ),
    )
    parser.add_argument("--summary-out", type=Path, default=DEFAULT_SUMMARY_OUT)
    parser.add_argument("--holdout-ratio", type=float, default=0.20)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Fixed team split (T02 manifest.json): put chats into dev/holdout strictly by its split field, no seed",
    )
    parser.add_argument(
        "--verify-manifest",
        type=Path,
        default=None,
        help=(
            "Compare the computed dev/holdout sets with this manifest.json and print counts and SHA-256 only "
            "(no IDs, nothing is written); exit code 1 on any mismatch"
        ),
    )
    return parser


def _main_verify(args: argparse.Namespace) -> int:
    try:
        expected = load_manifest(args.verify_manifest)
        if args.manifest is not None:
            dev, holdout, _ = compute_split(args.archive, assignment=load_manifest(args.manifest))
        else:
            dev, holdout, _ = compute_split(args.archive, seed=resolve_seed(), holdout_ratio=args.holdout_ratio)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    report = verify_against_manifest(set(dev["ids"]), set(holdout["ids"]), expected)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["exact_match"] else 1


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.verify_manifest is not None:
        return _main_verify(args)
    try:
        seed = None if args.manifest is not None else resolve_seed()
        holdout_dir = check_holdout_dir(args.holdout_dir if args.holdout_dir is not None else default_holdout_dir())
        if seed is None:
            _, _, summary = run_split_from_manifest(args.archive, args.manifest, holdout_dir, args.summary_out)
        else:
            _, _, summary = run_split(
                args.archive,
                holdout_dir,
                args.summary_out,
                seed=seed,
                holdout_ratio=args.holdout_ratio,
            )
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    if summary["cluster_overlap_count"]:
        print(
            f"warning: manifest splits {summary['cluster_overlap_count']} near-duplicate cluster(s) "
            "across dev and holdout"
        )
    print(
        f"Split {summary['total_records']} records -> "
        f"open_dev={summary['open_dev_count']} ({summary['open_dev_ratio']:.1%}), "
        f"private_holdout={summary['private_holdout_count']} ({summary['private_holdout_ratio']:.1%}), "
        f"clusters={summary['duplicate_clusters']['total_clusters']}, "
        f"overlap_count={summary['overlap_count']}, "
        f"holdout_path={holdout_dir / 'holdout_eval.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
