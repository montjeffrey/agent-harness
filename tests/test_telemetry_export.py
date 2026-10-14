"""Tests for harness.telemetry_export: cross-machine project keys and the
``export --to DIR`` layout. Everything runs under a tmp home and tmp dirs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness import telemetry, telemetry_export, telemetry_store


def _repo(root: Path, url: str | None) -> Path:
    git = root / ".git"
    git.mkdir(parents=True)
    config = '[core]\n\tbare = false\n[remote "upstream"]\n\turl = https://example.com/other/fork.git\n'
    if url:
        config += f'[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    config += '[branch "main"]\n\tremote = origin\n'
    (git / "config").write_text(config, encoding="utf-8")
    return root


# --- project keys -------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "git@github.com:Me/Repo.git",
    "https://github.com/me/repo",
    "https://user:token@github.com/me/repo.git/",
    "ssh://git@github.com:22/me/repo.git",
])
def test_normalize_remote_spellings_agree(url: str) -> None:
    assert telemetry_export.normalize_remote(url) == "github.com/me/repo"


def test_same_remote_gives_same_key_on_two_machines(tmp_path: Path) -> None:
    a = _repo(tmp_path / "machine-a" / "work" / "repo", "git@github.com:me/repo.git")
    b = _repo(tmp_path / "machine-b" / "src" / "renamed-clone", "https://github.com/me/repo")
    key_a = telemetry_export.compute_project_key(str(a))
    assert key_a.startswith("git:")
    assert key_a == telemetry_export.compute_project_key(str(b))
    # A subdirectory of the checkout is the same project.
    sub = a / "harness" / "collectors"
    sub.mkdir(parents=True)
    assert telemetry_export.compute_project_key(str(sub)) == key_a


def test_key_never_contains_the_path_or_url(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "secret-name", "git@github.com:me/secret-repo.git")
    key = telemetry_export.compute_project_key(str(repo))
    assert "secret" not in key


def test_worktree_resolves_to_the_main_checkout_remote(tmp_path: Path) -> None:
    main = _repo(tmp_path / "repo", "git@github.com:me/repo.git")
    (main / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    (main / ".git" / "worktrees" / "wt1" / "commondir").write_text("../..\n", encoding="utf-8")
    wt = main / ".claude" / "worktrees" / "wt1"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt1'}\n", encoding="utf-8")
    assert telemetry_export.compute_project_key(str(wt)) == telemetry_export.compute_project_key(str(main))


def test_no_remote_falls_back_to_basename_hash(tmp_path: Path) -> None:
    one = tmp_path / "a" / "Scratch"
    two = tmp_path / "b" / "scratch"
    one.mkdir(parents=True)
    two.mkdir(parents=True)
    key = telemetry_export.compute_project_key(str(one))
    assert key.startswith("dir:")
    assert key == telemetry_export.compute_project_key(str(two))
    # A Windows path recorded on another machine keys the same way.
    assert key == telemetry_export.compute_project_key("D:\\Users\\x\\scratch\\")
    # A repo whose only remote is not origin still keys by that remote.
    upstream_only = _repo(tmp_path / "c" / "scratch", None)
    assert telemetry_export.compute_project_key(str(upstream_only)).startswith("git:")


def test_keys_are_cached_and_survive_a_deleted_checkout(tmp_path: Path) -> None:
    import shutil

    repo = _repo(tmp_path / "repo", "git@github.com:me/repo.git")
    keys = telemetry_export.ProjectKeys.load()
    key = keys.key(str(repo))
    assert keys.labels[key] == "repo"
    keys.save()
    shutil.rmtree(repo)
    assert telemetry_export.compute_project_key(str(repo)).startswith("dir:")
    assert telemetry_export.ProjectKeys.load().key(str(repo)) == key


def test_existing_key_passes_through_unchanged() -> None:
    keys = telemetry_export.ProjectKeys()
    assert keys.key("git:0123456789abcdef") == "git:0123456789abcdef"
    assert keys.key(None) is None


# --- export -------------------------------------------------------------------


def _seed_store(project: Path) -> None:
    telemetry_store.append_events([
        {"agent": "claude-code", "event": "usage", "ts": "2026-09-30T23:00:00Z", "project": str(project),
         "session_id": "s-1", "source": "C:\\Users\\me\\.claude\\projects\\x\\s-1.jsonl", "input_tokens": 10},
        {"agent": "claude-code", "event": "usage", "ts": "2026-10-01T01:00:00Z", "project": str(project),
         "session_id": "s-1", "source": "C:\\Users\\me\\.claude\\projects\\x\\s-1.jsonl", "input_tokens": 20},
        {"agent": "codex", "event": "collector_error", "ts": "2026-10-01T02:00:00Z", "source": "boom at C:\\x"},
    ], now=datetime(2026, 10, 1, tzinfo=timezone.utc))


def test_export_layout_drops_source_and_keys_project(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", "git@github.com:me/repo.git")
    _seed_store(repo)
    out = tmp_path / "OneDrive" / "harness-telemetry"

    summary = telemetry_export.export_store(out)

    machine = telemetry_store.machine_identity()["id"]
    assert summary["machine"] == machine
    assert summary["records"] == 2  # collector_error left out
    assert sorted(p.name for p in (out / machine).iterdir()) == [
        "events-202609.jsonl", "events-202610.jsonl", "machine.json",
    ]
    records = list(telemetry_export.iter_export(out))
    assert len(records) == 2
    for rec in records:
        assert "source" not in rec
        assert rec["machine"] == machine
        assert rec["project"] == telemetry_export.compute_project_key(str(repo))
        assert str(tmp_path) not in json.dumps(rec)
    assert json.loads((out / machine / "machine.json").read_text(encoding="utf-8")) == {"id": machine}


def test_export_is_idempotent_and_leaves_unchanged_files_alone(tmp_path: Path) -> None:
    _seed_store(tmp_path / "proj")
    out = tmp_path / "export"
    telemetry_export.export_store(out)
    shard = next((out / telemetry_store.machine_identity()["id"]).glob("events-202610.jsonl"))
    before = shard.stat().st_mtime_ns

    again = telemetry_export.export_store(out)
    assert again["files_written"] == 0
    assert again["files_unchanged"] == 2
    assert shard.stat().st_mtime_ns == before
    assert len(list(telemetry_export.iter_export(out))) == 2


def test_export_carries_the_machine_label(tmp_path: Path) -> None:
    telemetry_store.set_machine_label("desk")
    _seed_store(tmp_path / "proj")
    out = tmp_path / "export"
    telemetry_export.export_store(out)
    assert telemetry_export.export_machines(out) == {telemetry_store.machine_identity()["id"]: "desk"}


def test_iter_export_merges_machines_and_names_unstamped_records(tmp_path: Path) -> None:
    out = tmp_path / "export"
    (out / "m-other").mkdir(parents=True)
    (out / "m-other" / "events-202610.jsonl").write_text(
        '{"schema": 1, "ts": "2026-10-02T00:00:00Z", "agent": "codex", "event": "usage", "session_id": "c"}\n'
        "not json\n",
        encoding="utf-8",
    )
    _seed_store(tmp_path / "proj")
    telemetry_export.export_store(out)
    machines = {r["machine"] for r in telemetry_export.iter_export(out)}
    assert machines == {"m-other", telemetry_store.machine_identity()["id"]}


def test_export_cli(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _seed_store(tmp_path / "proj")
    rc = telemetry.main(["export", "--to", str(tmp_path / "export")])
    assert rc == 0
    assert "exported 2 record(s)" in capsys.readouterr().out


def test_export_into_a_folder_past_max_path(tmp_path: Path) -> None:
    """A synced folder deep in a profile plus the temp sibling's suffix can
    pass Windows' 260-character limit; export and read-back must still work."""
    _seed_store(tmp_path / "proj")
    out = tmp_path / ("d" * 120) / ("e" * 100)
    summary = telemetry_export.export_store(out)
    assert summary["files_written"] == 2
    assert len(list(telemetry_export.iter_export(out))) == 2
    assert telemetry_export.export_store(out)["files_unchanged"] == 2


# --- automatic export target --------------------------------------------------

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)


def _run_in_process(argv: list[str]) -> None:
    """A fake spawner: run the background command here, synchronously."""
    assert argv[1] == str(Path(telemetry.__file__).resolve())
    telemetry.main(argv[2:])


def test_export_target_is_opt_in_and_stored_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert telemetry_store.export_target() is None
    telemetry_store.set_machine_label("desk")
    monkeypatch.chdir(tmp_path)

    data = telemetry_store.set_export_target("OneDrive/telemetry")

    assert telemetry_store.export_target() == tmp_path / "OneDrive" / "telemetry"
    assert data["label"] == "desk"  # setting the target keeps the rest of the card
    telemetry_store.set_machine_label("desk-2")
    assert telemetry_store.export_target() == tmp_path / "OneDrive" / "telemetry"
    telemetry_store.set_export_target("")
    assert telemetry_store.export_target() is None
    assert telemetry_store.machine_identity()["label"] == "desk-2"


def test_export_target_never_leaves_the_machine(tmp_path: Path) -> None:
    out = tmp_path / "export"
    telemetry_store.set_export_target(str(out))
    _seed_store(tmp_path / "proj")
    telemetry_export.export_store(out)
    card = json.loads((out / telemetry_store.machine_identity()["id"] / "machine.json").read_text(encoding="utf-8"))
    assert set(card) == {"id"}


def test_daily_run_adds_export_only_when_a_target_is_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(telemetry.AUTO_ENV, raising=False)
    started: list[list[str]] = []
    assert telemetry.maybe_start_daily_collect(now=NOW, spawn=started.append) is True
    assert started[-1][2:] == ["collect", "--quiet"]

    telemetry_store.set_export_target(str(tmp_path / "export"))
    assert telemetry.maybe_start_daily_collect(now=NOW + timedelta(hours=21), spawn=started.append) is True
    assert started[-1][2:] == ["collect", "--quiet", "--then-export"]


def test_daily_run_collects_then_exports_to_the_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(telemetry.AUTO_ENV, raising=False)
    _seed_store(tmp_path / "proj")
    out = tmp_path / "OneDrive" / "harness-telemetry"
    assert telemetry.main(["machine", "--label", "desk", "--export-to", str(out)]) == 0

    assert telemetry.maybe_start_daily_collect(now=NOW, spawn=_run_in_process) is True

    machine = telemetry_store.machine_identity()["id"]
    assert sorted(p.name for p in (out / machine).glob("events-*.jsonl")) == [
        "events-202609.jsonl", "events-202610.jsonl",
    ]
    assert telemetry_export.export_machines(out) == {machine: "desk"}
    assert not (telemetry_store.telemetry_dir() / ".collect.lock").exists()


def test_collect_then_export_reports_through_machine(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _seed_store(tmp_path / "proj")
    out = tmp_path / "export"
    telemetry.main(["machine", "--export-to", str(out)])
    assert "not exported there yet" in capsys.readouterr().out

    assert telemetry.main(["collect", "--quiet", "--then-export"]) == 0
    assert capsys.readouterr().out == ""

    telemetry.main(["machine"])
    text = capsys.readouterr().out
    assert f"daily export: {out}" in text
    assert "last export" in text and "2 record(s)" in text


def test_then_export_without_a_target_does_nothing(capsys: pytest.CaptureFixture) -> None:
    assert telemetry.main(["collect", "--then-export"]) == 0
    assert "no export target set" in capsys.readouterr().out
    assert not (telemetry_store.telemetry_dir() / ".auto_export.json").exists()


def test_a_failed_export_stays_silent_and_is_recorded(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _seed_store(tmp_path / "proj")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the synced folder should be", encoding="utf-8")
    telemetry_store.set_export_target(str(blocker / "telemetry"))

    assert telemetry.main(["collect", "--quiet", "--then-export"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert not (telemetry_store.telemetry_dir() / ".collect.lock").exists()

    telemetry.main(["machine"])
    assert "failed:" in capsys.readouterr().out


def test_then_export_waits_for_the_collect_lock(tmp_path: Path) -> None:
    _seed_store(tmp_path / "proj")
    out = tmp_path / "export"
    telemetry_store.set_export_target(str(out))
    lock = telemetry_store.telemetry_dir() / ".collect.lock"
    lock.write_text("123", encoding="utf-8")

    assert telemetry.main(["collect", "--quiet", "--then-export"]) == 0
    assert not out.exists()  # the run holding the lock owns the store; this one does nothing


def test_auto_export_into_a_folder_past_max_path(tmp_path: Path) -> None:
    _seed_store(tmp_path / "proj")
    out = tmp_path / ("d" * 120) / ("e" * 100)
    telemetry_store.set_export_target(str(out))
    assert telemetry.main(["collect", "--quiet", "--then-export"]) == 0
    assert len(list(telemetry_export.iter_export(out))) == 2
