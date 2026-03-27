from __future__ import annotations

from branchclaw.manual_threejs import (
    DEFAULT_REPO_URL,
    ThreejsLongrunHarness,
    build_iteration_plan,
    build_rules_text,
    build_spec_text,
    build_worker_task,
    default_max_iterations,
    dummy_env_local,
    fallback_report_status,
    iteration_label,
    summarize_changed_surface,
)


def test_iteration_label_formats_two_digits():
    assert iteration_label(1) == "iter-01"
    assert iteration_label(12) == "iter-12"


def test_default_max_iterations_matches_duration_policy():
    assert default_max_iterations(24) == 6
    assert default_max_iterations(48) == 12
    assert default_max_iterations(0.5) == 1


def test_dummy_env_local_contains_emailjs_keys():
    env_text = dummy_env_local()

    assert "VITE_APP_SERVICE_ID" in env_text
    assert "VITE_APP_TEMPLATE_ID" in env_text
    assert "VITE_APP_EMAIL" in env_text
    assert "VITE_APP_PUBLIC_KEY" in env_text


def test_threejs_longrun_text_builders_are_targeted():
    assert "React Three Fiber" in build_spec_text()
    assert "structured worker result" in build_rules_text()
    assert "Worker A owns scene visuals" in build_iteration_plan("iter-01")
    assert "port 4173" in build_worker_task("worker-a", 4173, "iter-01")
    assert "port 4174" in build_worker_task("worker-b", 4174, "iter-01")
    assert DEFAULT_REPO_URL.endswith("sanidhyy/threejs-portfolio.git")


def test_fallback_report_helpers_ignore_lockfiles_and_preserve_worker_focus():
    changed_files = [
        "package-lock.json",
        "src/sections/Hero.tsx",
        "src/components/Cube.tsx",
    ]

    summary = summarize_changed_surface("worker-a", changed_files)

    assert "3D scene" in summary
    assert "`src/sections/Hero.tsx`" in summary
    assert "package-lock.json" not in summary
    assert fallback_report_status(changed_files=changed_files, preview_url="") == "warning"
    assert fallback_report_status(changed_files=["package-lock.json"], preview_url="") == "blocked"


def test_threejs_harness_autoreports_missing_worker_results(monkeypatch, tmp_path):
    artifact_root = tmp_path / "artifacts"
    harness = ThreejsLongrunHarness(artifact_root=artifact_root, duration_hours=0.5, iteration_minutes=1)
    harness.run_id = "demo-run"

    calls: list[tuple[str, str]] = []

    def fake_submit(item, log_dir, label):
        calls.append((item["worker_name"], label))

    monkeypatch.setattr(harness, "_submit_fallback_worker_report", fake_submit)
    monkeypatch.setattr(
        harness,
        "_latest_run_snapshot",
        lambda _log_dir: {"workers": [{"worker_name": "worker-a", "result": {"status": "warning"}}]},
    )

    payload = {
        "workers": [
            {
                "worker_name": "worker-a",
                "workspace_path": str(tmp_path / "workspace"),
                "tmux_target": "demo:worker-a",
                "result": {},
            }
        ]
    }

    refreshed = harness._maybe_autoreport_workers(payload, artifact_root / "iter-01", "iter-01")

    assert calls == [("worker-a", "iter-01")]
    assert refreshed["workers"][0]["result"]["status"] == "warning"


def test_fallback_preview_url_prefers_latest_runtime_url(monkeypatch, tmp_path):
    harness = ThreejsLongrunHarness(artifact_root=tmp_path / "artifacts", duration_hours=0.5, iteration_minutes=1)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(
        harness,
        "_tmux_pane_text",
        lambda _target: (
            "Baseline preview=http://127.0.0.1:4172.\n"
            "VITE ready\n"
            "Local: http://127.0.0.1:4176/\n"
        ),
    )

    preview_url = harness._fallback_preview_url(workspace, "demo:worker-a")

    assert preview_url == "http://127.0.0.1:4176/"
