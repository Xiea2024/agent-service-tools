"""本地文档切分的离线测试：验证原文覆盖、出处、编号稳定与命令行导出。

只使用合成文本和临时 JSON 文件，不读取真实飞书正文、不请求模型或向量库。
测试重点是数据是否完整可追溯，不把切分成功当作检索效果已经通过。
"""

import json
import runpy
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from document_chunks import SourceDocument, chunk_document


def source(text: str, **changes) -> SourceDocument:
    """生成与飞书导出相同结构的演示资料，测试不依赖本机 .env。"""
    fields = {
        "document_id": "DocTest123",
        "title": "研发问题演示资料",
        "source_url": "https://demo.feishu.cn/wiki/Wiki123",
        "revision_id": 3,
        "content": text,
    }
    fields.update(changes)
    return SourceDocument.model_validate(fields)


@pytest.mark.parametrize(
    "content",
    [
        "接口返回500。先检查请求编号。\n\n再核对版本和日志。" * 20,
        "    def demo():\n        return {'status': 500}\n\n\t# 保留制表符和缩进\n" * 12,
        "重复段落。\n\n" * 100,
        "x" * 1200,  # 无段落、无空格的长文本也必须能降级到按字符切分。
        "\n\n  开头空白\n" + "中间内容。" * 25 + "\n\n   ",
        "开头" + " " * 180 + "结尾",  # 大段空白也不能导致中间内容定位错误。
    ],
)
def test_chunks_cover_original_text_exactly_and_respect_length(content):
    document = source(content)
    chunks = chunk_document(document, chunk_size=80, chunk_overlap=12)
    assert chunks
    # 根据位置把重复覆盖的部分去掉再拼回原文；包括换行、缩进和末尾空白。
    restored = ""
    for chunk in chunks:
        assert 0 < len(chunk.content) <= 80
        assert chunk.content == content[chunk.start_char : chunk.end_char]
        assert chunk.start_char <= len(restored)
        restored += chunk.content[len(restored) - chunk.start_char :]
    assert restored == content


def test_character_fallback_has_expected_overlap():
    chunks = chunk_document(source("abcdefghijklmnopqrstuvwxyz"), chunk_size=10, chunk_overlap=3)
    assert [chunk.content for chunk in chunks] == [
        "abcdefghij",
        "hijklmnopq",
        "opqrstuvwx",
        "vwxyz",
    ]
    assert [(chunk.start_char, chunk.end_char) for chunk in chunks] == [
        (0, 10),
        (7, 17),
        (14, 24),
        (21, 26),
    ]


def test_ids_are_repeatable_and_distinguish_same_text_in_different_positions_and_documents():
    document = source("abc" * 100)
    chunks = chunk_document(document, chunk_size=30, chunk_overlap=0)
    assert chunks == chunk_document(document, chunk_size=30, chunk_overlap=0)
    assert len({chunk.content for chunk in chunks}) == 1
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    assert all(UUID(chunk.chunk_id).version == 5 for chunk in chunks)
    for index, chunk in enumerate(chunks):
        assert chunk.chunk_index == index
        assert chunk.document_id == document.document_id
        assert chunk.title == document.title
        assert chunk.source_url == document.source_url
        assert chunk.revision_id == 3
    other = chunk_document(
        source(document.content, document_id="AnotherDoc"), chunk_size=30, chunk_overlap=0
    )
    assert {chunk.chunk_id for chunk in chunks}.isdisjoint(chunk.chunk_id for chunk in other)


def test_changed_text_or_parameters_produce_different_ids():
    first = chunk_document(source("短文本"))[0]
    changed = chunk_document(source("更改后的短文本"))[0]
    different_config = chunk_document(source("短文本"), chunk_size=300)[0]
    assert len({first.chunk_id, changed.chunk_id, different_config.chunk_id}) == 3


@pytest.mark.parametrize("content", ["", " \n\t "])
def test_empty_or_whitespace_document_produces_no_chunks(content):
    assert chunk_document(source(content)) == []


@pytest.mark.parametrize("size,overlap", [(0, 0), (-1, 0), (10, -1), (10, 10), (10, 11)])
def test_invalid_parameters_fail_explicitly(size, overlap):
    with pytest.raises(ValueError, match="chunk_size"):
        chunk_document(source("演示内容"), chunk_size=size, chunk_overlap=overlap)


@pytest.mark.parametrize("change", [{"content": 123}, {"document_id": ""}, {"revision_id": "3"}])
def test_bad_document_fields_are_not_silently_coerced(change):
    with pytest.raises(ValidationError):
        source("演示内容", **change)


def run_cli(monkeypatch, *arguments):
    """执行真实命令行入口，文件读写使用 pytest 临时目录。"""
    monkeypatch.setattr("sys.argv", ["chunk_feishu_document.py", *map(str, arguments)])
    script = Path(__file__).resolve().parents[1] / "scripts" / "chunk_feishu_document.py"
    runpy.run_path(str(script), run_name="__main__")


def test_cli_exports_reproducible_json_with_settings_and_source(monkeypatch, tmp_path, capsys):
    document = source("合成段落。\n\n" * 100)
    input_path = tmp_path / "document.json"
    input_path.write_text(document.model_dump_json(), encoding="utf-8")
    original = input_path.read_bytes()
    run_cli(monkeypatch, input_path, "--preview-chars", 0)
    output = tmp_path / "document.chunks.json"
    first_export = output.read_bytes()
    payload = json.loads(first_export)
    assert payload["chunking"] == {
        "strategy": "recursive-character-v1",
        "chunk_size": 500,
        "chunk_overlap": 80,
        "length_unit": "characters",
    }
    assert payload["document"]["document_id"] == document.document_id
    assert payload["document"]["character_count"] == len(document.content)
    assert payload["chunks"][0]["source_url"] == document.source_url
    assert "合成段落" not in capsys.readouterr().out
    run_cli(monkeypatch, input_path, "--preview-chars", 0)
    assert output.read_bytes() == first_export
    assert input_path.read_bytes() == original


def test_cli_rejects_output_that_would_overwrite_input(monkeypatch, tmp_path):
    path = tmp_path / "document.json"
    path.write_text(source("原始内容").model_dump_json(), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(SystemExit) as error:
        run_cli(monkeypatch, path, "--output", path)
    assert error.value.code == 2
    assert path.read_bytes() == original


@pytest.mark.parametrize("raw", ["not-json", '{"content":"来源字段缺失"}', "[]"])
def test_cli_invalid_document_fails_without_creating_output(monkeypatch, tmp_path, raw):
    path = tmp_path / "document.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        run_cli(monkeypatch, path)
    assert error.value.code == 1
    assert not (tmp_path / "document.chunks.json").exists()


def test_cli_empty_document_saves_empty_list_and_prints_hint(monkeypatch, tmp_path, capsys):
    path = tmp_path / "empty.json"
    path.write_text(source("").model_dump_json(), encoding="utf-8")
    output = tmp_path / "nested" / "chunks.json"
    run_cli(monkeypatch, path, "--output", output)
    assert json.loads(output.read_text(encoding="utf-8"))["chunks"] == []
    assert "正文为空" in capsys.readouterr().out


def test_cli_invalid_overlap_fails_without_export(monkeypatch, tmp_path):
    path = tmp_path / "document.json"
    path.write_text(source("演示内容").model_dump_json(), encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        run_cli(monkeypatch, path, "--chunk-size", 20, "--chunk-overlap", 20)
    assert error.value.code == 1
    assert not (tmp_path / "document.chunks.json").exists()
