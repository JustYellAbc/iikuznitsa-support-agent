"""split_chats.py with a fixed team manifest.json (T02): --manifest and --verify-manifest.

Synthetic archive and manifest in tmp_path only; nothing here touches the real holdout.
"""

from __future__ import annotations

import io
import json
import re
import secrets
import stat
import sys
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import split_chats  # noqa: E402 - needs the sys.path tweak above

TOPICS = (
    "лицензия антивируса для сервера",
    "продление подписки на почтовый шлюз",
    "коммерческое предложение на офисный пакет",
    "закрывающие документы по прошлому заказу",
    "сравнение редакций системы резервного копирования",
    "поставка ключей для удалённых сотрудников",
    "миграция с иностранного решения на отечественное",
    "сертификат ФСТЭК для средства защиты",
    "обучение администраторов работе с консолью",
    "расчёт стоимости на двести рабочих мест",
)
IDS = [910001 + i for i in range(len(TOPICS))]
CATEGORIES = ("Покупка", "Документы")


def _archive(tmp_path: Path) -> Path:
    lines = [
        f"#{cid} [{CATEGORIES[i % 2]}] [https://example.test/p{i}] {topic}"
        for i, (cid, topic) in enumerate(zip(IDS, TOPICS, strict=True))
    ]
    path = tmp_path / "chats.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("part_1.txt", "\n".join(lines) + "\n")
    return path


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _manifest(tmp_path: Path, holdout: set[int], name: str = "manifest.json") -> Path:
    rows = [{"chat_id": cid, "split": "holdout" if cid in holdout else "dev"} for cid in IDS]
    return _write(tmp_path / name, {"items": rows})


def _no_ids(text: str) -> bool:
    """No chat ID anywhere in text, not even inside a word; IDs embedded in a hex digest do not count."""
    return not any(re.search(rf"(?<![0-9a-f]){cid}(?![0-9a-f])", text) for cid in IDS)


def _main(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = split_chats.main(argv)
    return code, out.getvalue()


# ---------------------------------------------------------------------------
# load_manifest: layouts and errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("wrapper", [None, "items", "records", "rows", "chats"])
@pytest.mark.parametrize("id_key", ["id", "chat_id", "conversation_id"])
def test_manifest_layouts(tmp_path: Path, wrapper: str | None, id_key: str) -> None:
    rows = [
        {id_key: 910001, "split": "dev"},
        {id_key: "910002", "split": "holdout"},
        {id_key: "#910003", "split": "private_holdout"},
        {id_key: 910004, "split": "open_dev"},
    ]
    path = _write(tmp_path / "m.json", rows if wrapper is None else {wrapper: rows, "version": 1})
    assert split_chats.load_manifest(path) == {910001: "dev", 910002: "holdout", 910003: "holdout", 910004: "dev"}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([{"id": 910001, "split": "dev"}, {"id": 910001, "split": "holdout"}], "duplicate id"),
        ([{"id": 910001, "split": "dev"}, {"chat_id": "910001", "split": "dev"}], "duplicate id"),
        ([{"split": "dev"}], "expected exactly one of"),
        ([{"id": 910001, "chat_id": 910002, "split": "dev"}], "expected exactly one of"),
        ([{"id": 910001}], "split must be one of"),
        ([{"id": 910001, "split": "test"}], "split must be one of"),
        ([{"id": True, "split": "dev"}], "id must be an integer"),
        ([{"id": "abc", "split": "dev"}], "id must be an integer"),
        (["910001"], "expected an object"),
        ({"cases": [{"id": 910001, "split": "dev"}]}, "unknown manifest layout"),
        ({"items": [], "rows": []}, "unknown manifest layout"),
        ({"items": {"id": 910001}}, "unknown manifest layout"),
        ("910001", "unknown manifest layout"),
        ([], "no entries"),
    ],
)
def test_manifest_errors_are_explicit_and_hide_ids(tmp_path: Path, payload: object, message: str) -> None:
    with pytest.raises(split_chats.ManifestError, match=message) as exc_info:
        split_chats.load_manifest(_write(tmp_path / "m.json", payload))
    assert "910001" not in str(exc_info.value)


def test_manifest_must_cover_archive_exactly(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    missing = _write(tmp_path / "missing.json", [{"id": cid, "split": "dev"} for cid in IDS[:-1]])
    with pytest.raises(split_chats.ManifestError, match="1 archive chat"):
        split_chats.compute_split(archive, assignment=split_chats.load_manifest(missing))
    extra = _write(tmp_path / "extra.json", [{"id": cid, "split": "dev"} for cid in [*IDS, 999999]])
    with pytest.raises(split_chats.ManifestError, match="1 manifest id"):
        split_chats.compute_split(archive, assignment=split_chats.load_manifest(extra))


# ---------------------------------------------------------------------------
# --manifest: fixed split, same isolation
# ---------------------------------------------------------------------------


def test_manifest_split_follows_manifest_and_keeps_permissions(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    holdout = {IDS[1], IDS[4], IDS[7]}
    out_dir = tmp_path / "private"
    dev, hold, summary = split_chats.run_split_from_manifest(
        archive, _manifest(tmp_path, holdout), out_dir, tmp_path / "summary.json"
    )
    assert set(hold["ids"]) == holdout
    assert set(dev["ids"]) == set(IDS) - holdout
    assert [c["chat_id"] for c in hold["cases"]] == sorted(holdout)
    assert all(c["suite"] == "holdout" for c in hold["cases"])
    assert summary["split_source"] == "manifest"
    assert summary["private_holdout_count"] == 3 and summary["open_dev_count"] == 7
    assert summary["overlap_count"] == 0
    assert _no_ids((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700
    for name in ("dev_ids.json", "holdout_eval.json"):
        assert stat.S_IMODE((out_dir / name).stat().st_mode) == 0o600


def test_manifest_mode_needs_no_seed_but_keeps_holdout_dir_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EVAL_SPLIT_SEED", raising=False)
    manifest = _manifest(tmp_path, {IDS[0]})
    with pytest.raises(SystemExit, match="temp directory"):
        split_chats.main(
            ["--archive", str(_archive(tmp_path)), "--manifest", str(manifest), "--holdout-dir", str(tmp_path / "h")]
        )
    with pytest.raises(SystemExit, match="outside the repository"):
        split_chats.main(["--manifest", str(manifest), "--holdout-dir", str(REPO_ROOT / ".local/h")])


# ---------------------------------------------------------------------------
# --verify-manifest: counts and hashes only, non-zero exit on mismatch
# ---------------------------------------------------------------------------


def test_verify_exact_match_for_seeded_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproducibility: the reviewer's own seed regenerates the split recorded in the team manifest."""
    archive = _archive(tmp_path)
    seed = secrets.token_hex(16)
    _, hold, _ = split_chats.compute_split(archive, seed=seed)
    manifest = _manifest(tmp_path, set(hold["ids"]))
    monkeypatch.setenv("EVAL_SPLIT_SEED", seed)

    code, out = _main(["--archive", str(archive), "--verify-manifest", str(manifest)])
    report = json.loads(out)
    assert code == 0
    assert report["exact_match"] is True
    assert report["mismatch_count"] == 0
    assert report["computed"] == report["manifest"] == {"dev": 8, "holdout": 2}
    assert report["sha256"]["computed_dev"] == report["sha256"]["manifest_dev"]
    assert report["sha256"]["computed_holdout"] == report["sha256"]["manifest_holdout"]
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in report["sha256"].values())
    assert _no_ids(out)
    assert not list(tmp_path.glob("**/holdout_eval.json"))  # verification writes nothing


def test_verify_detects_swapped_members_with_equal_counts(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    fixed = _manifest(tmp_path, {IDS[2], IDS[5]}, "fixed.json")
    swapped = _manifest(tmp_path, {IDS[2], IDS[6]}, "swapped.json")  # same sizes, one member swapped

    code, out = _main(["--archive", str(archive), "--manifest", str(fixed), "--verify-manifest", str(swapped)])
    report = json.loads(out)
    assert code == 1
    assert report["exact_match"] is False
    assert report["computed"] == report["manifest"] == {"dev": 8, "holdout": 2}
    assert report["mismatch_count"] == 2
    assert report["sha256"]["computed_holdout"] != report["sha256"]["manifest_holdout"]
    assert report["sha256"]["computed_dev"] != report["sha256"]["manifest_dev"]
    assert _no_ids(out)

    code, out = _main(["--archive", str(archive), "--manifest", str(fixed), "--verify-manifest", str(fixed)])
    assert code == 0 and json.loads(out)["exact_match"] is True and _no_ids(out)


def test_verify_counts_ids_missing_on_either_side(tmp_path: Path) -> None:
    computed_dev, computed_holdout = set(IDS[:8]), set(IDS[8:])
    manifest = {cid: "dev" for cid in IDS[:7]} | {IDS[8]: "holdout", IDS[9]: "holdout", 999999: "dev"}
    report = split_chats.verify_against_manifest(computed_dev, computed_holdout, manifest)
    assert report["exact_match"] is False
    assert report["mismatch_count"] == 2  # IDS[7] only computed, 999999 only in manifest
    assert _no_ids(json.dumps(report))


def test_verify_reports_manifest_errors_without_ids(tmp_path: Path) -> None:
    bad = _write(tmp_path / "bad.json", [{"id": IDS[0], "split": "dev"}, {"id": IDS[0], "split": "holdout"}])
    with pytest.raises(SystemExit, match="duplicate id") as exc_info:
        split_chats.main(["--archive", str(_archive(tmp_path)), "--verify-manifest", str(bad)])
    assert _no_ids(str(exc_info.value))
