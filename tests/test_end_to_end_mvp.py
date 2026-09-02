from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import yaml
from PIL import Image
from test_render_cli import probe

BRIEF_TARGET_DURATION_SECONDS = 240


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run([str(executable), *args], check=False, capture_output=True, text=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def catalog_records(workspace: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (workspace / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def build_seeded_workspace(
    tmp_path: Path,
    *,
    photo_count: int = 3,
    vision_model: str = "fixture-v1",
    narrative_model: str = "fixture-success",
) -> tuple[Path, list[str]]:
    workspace = tmp_path / "wedding-e2e"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    config_path = workspace / "project.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["adapters"]["vision"] = {"name": "fake", "model": vision_model, "prompt_version": "v1"}
    data["adapters"]["narrative"] = {
        "name": "fake",
        "model": narrative_model,
        "prompt_version": "v1",
    }
    config_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    materials = workspace / "materials"
    materials.mkdir()
    colors = [(200, 100, 50), (40, 160, 200), (90, 200, 90), (210, 210, 60)]
    for index in range(photo_count):
        Image.new("RGB", (400, 300), colors[index % len(colors)]).save(
            materials / f"photo-{index}.jpg"
        )
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    interview_dir = workspace / "interview"
    interview_dir.mkdir()
    (interview_dir / "brief.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "couple": {
                    "partner_a": {"name": "Jane Doe", "called_as": "Jane"},
                    "partner_b": {"name": "Alex Chen", "called_as": "Alex"},
                    "first_met": "at a mutual friend's birthday party",
                    "relationship_years": 4,
                    "proposal": "on a hike at sunrise",
                    "turning_point": "moving in together during the pandemic",
                },
                "wedding": {
                    "date": "2026-11-01",
                    "venue": "Lakeside Hall",
                    "ceremony_style": "casual outdoor ceremony",
                    "guests": "close family and friends, about 60 people",
                    "screening_moment": "reception, after dinner",
                },
                "film": {
                    "target_duration_seconds": BRIEF_TARGET_DURATION_SECONDS,
                    "audience": "family and friends at the reception",
                    "tone_wanted": "warm and a little funny",
                    "tone_avoided": "overly sentimental",
                    "music": "acoustic guitar",
                },
                "constraints": {
                    "forbidden_topics": [],
                    "excluded_people": [],
                    "excluded_materials": [],
                    "notes": "keep it light",
                },
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    records = sorted(catalog_records(workspace), key=lambda record: record["locators"][0])
    asset_ids = [record["asset_id"] for record in records]
    return workspace, asset_ids


def test_full_pipeline_completes_through_the_public_cli_with_preserved_asset_hashes(
    tmp_path: Path,
) -> None:
    """One connected run: init -> scan -> extract -> analyze -> human feedback ->
    story/script/storyboard generate+adopt -> validate -> render -> status, plus the
    hash-preservation, delivery-contract, and rerender checks ticket #25 requires."""
    workspace, asset_ids = build_seeded_workspace(tmp_path, photo_count=3)
    materials = workspace / "materials"
    original_hashes = {path.name: sha256_bytes(path.read_bytes()) for path in materials.iterdir()}

    assert run_cli("--project", str(workspace), "catalog", "extract").returncode == 0

    analyzed = run_cli("--project", str(workspace), "catalog", "analyze-batch")
    assert analyzed.returncode == 0, analyzed.stdout
    assert "succeeded=3 reused=0 failed=0" in analyzed.stdout

    # Human feedback: a Participant, a Correction, and a Subject Attribution.
    added_participant = run_cli(
        "--project", str(workspace), "participant", "add",
        "--id", "bride-jane", "--display-name", "Jane Doe", "--role", "Bride", "--principal",
    )
    assert added_participant.returncode == 0, added_participant.stderr

    correction = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/inferences/wedding_moment", "--value", '"ceremony"',
        "--asset-id", asset_ids[0], "--actor", "test-reviewer", "--reason", "reviewed by human",
    )
    assert correction.returncode == 0, correction.stderr

    attribution = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/subject_attributions", "--value", '["bride-jane"]',
        "--asset-id", asset_ids[0], "--actor", "test-reviewer",
    )
    assert attribution.returncode == 0, attribution.stderr

    assert run_cli("--project", str(workspace), "story", "generate", "--json").returncode == 0
    assert run_cli("--project", str(workspace), "story", "adopt", "--json").returncode == 0
    story_frontmatter = yaml.safe_load(
        (workspace / "story.md").read_text(encoding="utf-8").split("---")[1]
    )
    assert story_frontmatter["target_duration_seconds"] == BRIEF_TARGET_DURATION_SECONDS
    assert run_cli("--project", str(workspace), "script", "generate", "--json").returncode == 0
    assert run_cli("--project", str(workspace), "script", "adopt", "--json").returncode == 0
    assert run_cli("--project", str(workspace), "storyboard", "generate", "--json").returncode == 0
    assert run_cli("--project", str(workspace), "storyboard", "adopt", "--json").returncode == 0

    full_validation = run_cli("--project", str(workspace), "validate", "--json")
    assert full_validation.returncode == 0, full_validation.stdout

    storyboard_validation = json.loads(
        run_cli("--project", str(workspace), "storyboard", "validate", "--json").stdout
    )
    expected_frames = storyboard_validation["document"]["total_frames"]

    rendered = run_cli("--project", str(workspace), "render", "rough-cut")
    assert rendered.returncode == 0, rendered.stderr
    artifact = workspace / "renders" / "rough-cut.mp4"
    assert artifact.is_file()
    first_bytes = artifact.read_bytes()

    payload = probe(artifact)
    streams = payload["streams"]
    assert len(streams) == 1, "expected exactly one stream: video only, no audio"
    video = streams[0]
    assert video["codec_type"] == "video"
    assert video["codec_name"] == "h264"
    assert video["width"] == 1920
    assert video["height"] == 1080
    assert video["pix_fmt"] == "yuv420p"
    assert video["r_frame_rate"] == "24/1"
    assert video["avg_frame_rate"] == "24/1"
    assert int(video["nb_read_frames"]) == expected_frames
    assert "mp4" in payload["format"]["format_name"].split(",")

    final_status = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)
    assert final_status["state"] in ("ready", "complete-with-warnings")
    for layer in ("materials", "semantic_catalog", "story", "script", "storyboard", "rough_cut"):
        assert final_status["layers"][layer]["state"] in ("ready", "complete-with-warnings")

    current_hashes = {path.name: sha256_bytes(path.read_bytes()) for path in materials.iterdir()}
    assert current_hashes == original_hashes

    canonical_names = ("catalog.jsonl", "story.md", "script.md", "storyboard.yaml")
    pre_rerender_snapshot = {
        name: (workspace / name).read_bytes() for name in canonical_names
    }

    rerendered = run_cli("--project", str(workspace), "render", "rough-cut")
    assert rerendered.returncode == 0, rerendered.stderr
    assert artifact.read_bytes() == first_bytes
    for name, snapshot in pre_rerender_snapshot.items():
        assert (workspace / name).read_bytes() == snapshot

    post_rerender_hashes = {
        path.name: sha256_bytes(path.read_bytes()) for path in materials.iterdir()
    }
    assert post_rerender_hashes == original_hashes


def test_partial_provider_failure_preserves_completed_analysis_checkpoints(
    tmp_path: Path,
) -> None:
    workspace, _ = build_seeded_workspace(tmp_path, photo_count=4)
    assert run_cli("--project", str(workspace), "catalog", "extract").returncode == 0

    capped = run_cli(
        "--project", str(workspace), "catalog", "analyze-batch",
        "--max-estimated-usd", "0.02",
    )
    assert capped.returncode == 2, capped.stdout
    assert "succeeded=2 reused=0 failed=0" in capped.stdout
    assert "budget_stopped=True" in capped.stdout

    records = catalog_records(workspace)
    analyzed_ids = sorted(
        record["asset_id"] for record in records if record.get("inferences")
    )
    pending_ids = sorted(
        record["asset_id"] for record in records if not record.get("inferences")
    )
    assert len(analyzed_ids) == 2
    assert len(pending_ids) == 2

    reuse = run_cli(
        "--project", str(workspace), "catalog", "analyze-batch", "--asset-id", analyzed_ids[0]
    )
    assert reuse.returncode == 0, reuse.stdout
    assert "succeeded=0 reused=1 failed=0" in reuse.stdout

    completed = run_cli("--project", str(workspace), "catalog", "analyze-batch")
    assert completed.returncode == 0, completed.stdout
    assert "succeeded=2 reused=0 failed=0" in completed.stdout

    final_records = catalog_records(workspace)
    assert all(record.get("inferences") for record in final_records)


def test_failed_rerender_preserves_prior_rough_cut_and_every_canonical_source(
    tmp_path: Path,
) -> None:
    workspace, _ = build_seeded_workspace(tmp_path, photo_count=2)
    assert run_cli("--project", str(workspace), "story", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "story", "adopt").returncode == 0
    assert run_cli("--project", str(workspace), "script", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "script", "adopt").returncode == 0
    assert run_cli("--project", str(workspace), "storyboard", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "storyboard", "adopt").returncode == 0

    first = run_cli("--project", str(workspace), "render", "rough-cut")
    assert first.returncode == 0, first.stderr
    artifact = workspace / "renders" / "rough-cut.mp4"
    prior_bytes = artifact.read_bytes()

    canonical_names = ("catalog.jsonl", "story.md", "script.md", "storyboard.yaml")
    canonical_snapshot = {name: (workspace / name).read_bytes() for name in canonical_names}
    materials = workspace / "materials"
    untouched_asset = materials / "photo-1.jpg"
    untouched_bytes = untouched_asset.read_bytes()

    # Simulate a corrupted Original Asset surfacing only at the next render preflight.
    corrupted_asset = materials / "photo-0.jpg"
    corrupted_asset.write_bytes(b"not a real image after simulated corruption")

    second = run_cli("--project", str(workspace), "render", "rough-cut")

    assert second.returncode == 1
    assert "CATALOG_SOURCE_INTEGRITY" in second.stderr
    assert artifact.read_bytes() == prior_bytes
    for name, snapshot in canonical_snapshot.items():
        assert (workspace / name).read_bytes() == snapshot
    assert untouched_asset.read_bytes() == untouched_bytes


def test_a_workspace_under_the_conventional_projects_directory_is_never_tracked_by_git(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    relative = Path("projects") / f"e2e-git-check-{uuid.uuid4().hex[:8]}"
    workspace = repo_root / relative
    try:
        assert run_cli("--project", str(workspace), "project", "init").returncode == 0
        materials = workspace / "materials"
        materials.mkdir()
        Image.new("RGB", (40, 30), (10, 20, 30)).save(materials / "private-photo.jpg")
        assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0

        status = subprocess.run(
            ["git", "status", "--porcelain", "--", str(relative)],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert status.stdout.strip() == ""

        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(relative / "materials" / "private-photo.jpg")],
            cwd=repo_root,
            check=False,
        )
        assert ignored.returncode == 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
