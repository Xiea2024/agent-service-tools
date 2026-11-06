import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from evaluation import Case, Suite, Turn
from evaluation_jobs import EvaluationJobStore

ROOT = Path(__file__).resolve().parents[1]


def test_task_query_reports_location_without_model_calls(tmp_path, monkeypatch, capsys):
    store = EvaluationJobStore(tmp_path / "jobs.sqlite3", "app", "dev-issue-agent")
    suite = Suite(
        id="fixed",
        version="1",
        description="测试",
        cases=[
            Case(
                id="one",
                title="测试",
                category="test",
                turns=[Turn(message="问题", rubric=["复核"])],
            )
        ],
    )
    job, _ = store.create("event", "user", suite, tmp_path / "runs")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args: None)
    monkeypatch.setattr(
        "sys.argv",
        ["show_evaluation_job.py", job.job_id, "--db", str(store.path), "--app-id", "app"],
    )
    runpy.run_path(str(ROOT / "scripts/show_evaluation_job.py"), run_name="__main__")
    output = json.loads(capsys.readouterr().out)
    assert output["output_dir"] == job.output_dir and output["status"] == "queued"
    assert "suite_json" not in output and output["summary"] == {}


@pytest.mark.parametrize("fail", [False, True])
def test_publish_cli_retains_receipt_and_never_evaluates(tmp_path, monkeypatch, capsys, fail):
    (tmp_path / "summary.json").write_text(
        json.dumps({"status": "completed", "evaluation_id": "run-123"}), encoding="utf-8"
    )
    (tmp_path / "report.md").write_text("# 原报告", encoding="utf-8")
    settings = SimpleNamespace(
        FEISHU_APP_ID="app",
        FEISHU_APP_SECRET=SimpleNamespace(get_secret_value=lambda: "fake"),
        FEISHU_REPORT_FOLDER_TOKEN="",
    )
    monkeypatch.setattr("run_feishu.FeishuSettings", lambda: settings)
    monkeypatch.setattr("run_feishu.FeishuHTTPTransport", Mock())

    def publish(path, title, owner, created):
        assert path == tmp_path / "report.md" and owner == "user"
        created("doc123")
        if fail:
            raise RuntimeError("权限失败")
        return "https://tenant.feishu.cn/docx/doc123"

    publisher = Mock()
    publisher.publish.side_effect = publish
    monkeypatch.setattr("feishu_reports.FeishuReportPublisher", Mock(return_value=publisher))
    runner = Mock(side_effect=AssertionError("发布旧报告不应重新评测"))
    monkeypatch.setattr("evaluation.run_evaluation", runner)
    monkeypatch.setattr(
        "sys.argv",
        ["publish_evaluation_report.py", "--run-dir", str(tmp_path), "--open-id", "user"],
    )
    if fail:
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(ROOT / "scripts/publish_evaluation_report.py"), run_name="__main__")
        assert exc.value.code == 1
    else:
        runpy.run_path(str(ROOT / "scripts/publish_evaluation_report.py"), run_name="__main__")
        assert "已核对" in capsys.readouterr().out
    receipt = json.loads((tmp_path / "publication.json").read_text("utf-8"))
    assert receipt["document_id"] == "doc123"
    assert receipt["status"] == ("failed" if fail else "succeeded")
    assert (tmp_path / "report.md").read_text("utf-8") == "# 原报告"
    runner.assert_not_called()
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "scripts/publish_evaluation_report.py"), run_name="__main__")
    assert exc.value.code == 2
    publisher.publish.assert_called_once()
    new_receipt = tmp_path / "publication-retry.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "publish_evaluation_report.py",
            "--run-dir",
            str(tmp_path),
            "--open-id",
            "user",
            "--receipt",
            str(new_receipt),
        ],
    )
    if fail:
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(ROOT / "scripts/publish_evaluation_report.py"), run_name="__main__")
        assert exc.value.code == 1
    else:
        runpy.run_path(str(ROOT / "scripts/publish_evaluation_report.py"), run_name="__main__")
    assert json.loads((tmp_path / "publication.json").read_text("utf-8")) == receipt
    assert json.loads(new_receipt.read_text("utf-8"))["document_id"] == "doc123"
    runner.assert_not_called()
