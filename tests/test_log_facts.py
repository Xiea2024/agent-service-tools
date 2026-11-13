"""日志事实解析：确定性能提取什么、不能提取什么。"""

from log_facts import MAX_CHARS, parse_log_facts

PYTHON_LOG = """2026-11-12 10:03:21,114 ERROR [http-nio-8080-exec-3] request_id=abc123def456
Traceback (most recent call last):
  File "/srv/app/handler.py", line 42, in handle
    return downstream.call(payload)
  File "/srv/app/client.py", line 88, in call
    raise TimeoutError("upstream timeout")
TimeoutError: upstream timeout
"""

JAVA_LOG = """2026-11-12 10:03:21.114 ERROR 1 --- [nio-8080-exec-3] c.e.OrderController : status=500
java.lang.IllegalStateException: Missing property: spring.datasource.url
    at com.example.OrderService.init(OrderService.java:31)
"""

CHAT = "订单接口返回 500，升级后开始出现的，怎么排查？"


def test_python_traceback_extracts_facts():
    parsed = parse_log_facts(PYTHON_LOG)
    assert parsed.facts["timestamp"].startswith("2026-11-12 10:03:21")
    assert parsed.facts["level"] == "ERROR"
    assert parsed.facts["request_id"] == "abc123def456"
    assert parsed.facts["exception"] == "TimeoutError"
    assert parsed.facts["stack_frames"][0].startswith("/srv/app/handler.py:42")
    assert parsed.truncated is False


def test_java_stack_extracts_status_and_frame():
    parsed = parse_log_facts(JAVA_LOG)
    assert parsed.facts["status_code"] == 500
    assert parsed.facts["exception"] == "IllegalStateException"
    assert parsed.facts["stack_frames"][0].startswith("com.example.OrderService.init(")


def test_plain_question_is_not_treated_as_log():
    parsed = parse_log_facts(CHAT)
    assert "exception" not in parsed.facts
    assert "stack_frames" not in parsed.facts
    assert any("异常类型" in warning for warning in parsed.warnings)


def test_empty_text_warns_instead_of_returning_fake_facts():
    parsed = parse_log_facts("   \n  ")
    assert parsed.facts == {}
    assert parsed.warnings == ["日志为空，没有任何可解析内容"]
    assert parsed.as_prompt_block().startswith("以下内容是程序正则解析得到的确定事实")


def test_missing_fields_are_reported_not_guessed():
    parsed = parse_log_facts("ERROR boom happened")
    assert parsed.facts["level"] == "ERROR"
    assert "timestamp" not in parsed.facts
    warnings = " ".join(parsed.warnings)
    assert "未识别到时间戳" in warnings
    assert "未识别到异常类型或 HTTP 状态码" in warnings


def test_duplicate_request_ids_report_a_warning():
    parsed = parse_log_facts("request_id=aaaaaaaa11\nrequest_id=bbbbbbbb22")
    assert parsed.facts["request_id"] == "aaaaaaaa11"
    assert any("只采用第一个" in warning for warning in parsed.warnings)


def test_warning_branch_suggests_pasting_the_raw_error():
    parsed = parse_log_facts("今天下午突然变慢")
    assert any("可能不是日志" in warning for warning in parsed.warnings)


def test_long_log_is_truncated_and_says_so():
    parsed = parse_log_facts("x" * (MAX_CHARS + 10))
    assert parsed.truncated is True
    assert any("只解析前" in warning for warning in parsed.warnings)


def test_prompt_block_is_explicit_about_empty_results():
    block = parse_log_facts("2026-11-12 10:00:00 ERROR x").as_prompt_block()
    assert "不是模型推断" in block
    assert "- level: ERROR" in block
    assert "解析警告" in block
