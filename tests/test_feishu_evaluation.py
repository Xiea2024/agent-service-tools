import json
from threading import Event
from unittest.mock import Mock

import pytest

from evaluation import Case, Suite, Turn
from evaluation_jobs import EvaluationJobStore, EvaluationWorker
from feishu_evaluation import EvaluationAction, FeishuEvaluationUI, evaluation_card
from run_feishu import FeishuHandler


@pytest.fixture
def ui(tmp_path):
    suite = Suite(
        id="fixed",
        version="1",
        description="测试",
        cases=[
            Case(
                id="one",
                title="案例",
                category="test",
                turns=[Turn(message="问题", rubric=["复核"])],
            )
        ],
    )
    cases = tmp_path / "cases.json"
    cases.write_text(suite.model_dump_json(), encoding="utf-8")
    store = EvaluationJobStore(tmp_path / "jobs.sqlite3", "app", "dev-issue-agent")
    worker = EvaluationWorker(store, Mock(), Mock(), Mock())
    return FeishuEvaluationUI(
        store, worker, cases, tmp_path / "runs", Mock(return_value=("card", "chat"))
    )


def menu(action="eval_start", event_id="menu-1", owner="user"):
    return {
        "header": {"event_id": event_id},
        "event": {
            "event_key": action,
            "operator": {"operator_id": {"open_id": owner}},
        },
    }


def test_menu_only_creates_job_and_does_not_run_models_on_reply_thread(ui):
    handler = FeishuHandler(Mock(), evaluation_ui=ui)
    handler.on_menu_event(menu())
    handler.start()
    try:
        handler._queue.join()
    finally:
        handler.stop()
    job = ui.store.latest("user")
    assert job.status == "queued"
    ui.worker.evaluate.assert_not_called()
    assert ui.send_card.call_args.args[:2] == ("open_id", "user")
    assert ui.worker.wake.is_set()


def test_duplicate_start_and_status_share_one_saved_job(ui):
    ui.handle(EvaluationAction("start", "user", "eval_start"))
    job = ui.store.latest("user")
    ui.handle(EvaluationAction("again", "user", "eval_start"))
    assert "未重复启动" in json.dumps(ui.send_card.call_args.args[2], ensure_ascii=False)
    ui.handle(EvaluationAction("status", "user", "eval_status"))
    assert job.job_id in json.dumps(ui.send_card.call_args.args[2])
    assert ui.store.latest("user").job_id == job.job_id


def test_status_does_not_disclose_other_owners_job(ui):
    ui.handle(EvaluationAction("start", "user", "eval_start"))
    job = ui.store.latest("user")
    ui.handle(EvaluationAction("foreign", "other", "eval_status", job.job_id))
    sent = ui.send_card.call_args.args
    assert sent[:2] == ("open_id", "other")
    assert job.job_id not in json.dumps(sent[2])


def test_optional_operator_allowlist_blocks_creation(ui):
    ui.allowed_open_ids = {"operator"}
    ui.handle(EvaluationAction("start", "user", "eval_start"))
    assert ui.store.latest("user") is None
    assert "未被配置" in json.dumps(ui.send_card.call_args.args[2], ensure_ascii=False)


def test_callback_status_is_queued_and_missing_identifiers_are_rejected(ui):
    handler = FeishuHandler(Mock(), evaluation_ui=ui)
    event = {
        "header": {"event_id": "callback"},
        "event": {
            "operator": {"open_id": "user"},
            "action": {"value": {"action": "eval_status", "job_id": "job"}},
        },
    }
    assert handler.on_card_action(event)["toast"]["type"] == "info"
    queued = handler._queue.get_nowait()
    assert queued == EvaluationAction("callback", "user", "eval_status", "job")
    event["event"]["operator"] = {}
    assert handler.on_card_action(event)["toast"]["type"] == "error"


def test_report_failure_card_is_honest_and_success_card_has_url(ui):
    ui.handle(EvaluationAction("start", "user", "eval_start"))
    job = ui.store.latest("user")
    data = {"passed_cases": 0, "total_cases": 1, "turn_counts": {"error": 0}, "duration_seconds": 2}
    failed = ui.store.update(job.job_id, status="publish_failed", summary_json=json.dumps(data))
    card = evaluation_card(failed)
    assert card["header"]["title"]["content"] == "评测完成，报告发布失败"
    assert "本地报告已保留" in json.dumps(card, ensure_ascii=False)
    assert "open_url" not in json.dumps(card)
    success = ui.store.update(
        job.job_id, status="succeeded", report_url="https://tenant.feishu.cn/docx/doc123"
    )
    card = evaluation_card(success)
    assert "open_url" in json.dumps(card)
    assert card["body"]["elements"][-2]["behaviors"][0]["default_url"] == success.report_url


def test_background_work_does_not_block_status_or_normal_messages(ui):
    entered, release = Event(), Event()

    def evaluate(job, stop):
        entered.set()
        release.wait(2)
        return {
            "passed_cases": 1,
            "total_cases": 1,
            "turn_counts": {"error": 0},
            "duration_seconds": 1,
        }

    ui.worker.evaluate = evaluate
    ui.worker.publish = lambda job, stop: "https://tenant.feishu.cn/docx/doc123"
    text_sender = Mock()
    handler = FeishuHandler(text_sender, lambda _: "普通回答", evaluation_ui=ui)
    ui.worker.start()
    handler.start()
    try:
        handler.on_menu_event(menu())
        handler._queue.join()
        assert entered.wait(1)
        handler.on_menu_event(menu("eval_status", "status"))
        handler.on_event(
            {
                "event": {
                    "sender": {"sender_type": "user", "sender_id": {"open_id": "user"}},
                    "message": {
                        "message_id": "incoming",
                        "chat_id": "chat",
                        "chat_type": "p2p",
                        "message_type": "text",
                        "content": '{"text":"你好"}',
                    },
                }
            }
        )
        handler._queue.join()
        text_sender.assert_called_once_with("incoming", "普通回答")
        assert ui.store.latest("user").status == "running"
    finally:
        release.set()
        handler.stop()
        ui.worker.stop()


def test_completion_card_distinguishes_execution_errors_from_rule_failures(ui):
    ui.handle(EvaluationAction("start", "user", "eval_start"))
    job = ui.store.latest("user")
    summary = {
        "passed_cases": 0,
        "total_cases": 1,
        "turn_counts": {"error": 1},
        "duration_seconds": 2,
    }
    saved = ui.store.update(
        job.job_id,
        status="succeeded",
        summary_json=json.dumps(summary),
        report_url="https://tenant.feishu.cn/docx/doc123",
    )
    card = evaluation_card(saved)
    assert "含执行错误" in card["header"]["title"]["content"]
    assert "部分轮次执行失败" in json.dumps(card, ensure_ascii=False)
