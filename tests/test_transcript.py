import json

from claude_hub.transcript import first_cwd, human_text, iter_turns, parse_meta

from conftest import make_lines, to_jsonl


def lines_of(objs):
    return to_jsonl(objs).splitlines(keepends=True)


def test_meta_from_transcript():
    meta = parse_meta(lines_of(make_lines("s1", "/work/app", ["Fix the bug", "Now add a test"])))
    assert meta.session_id == "s1"
    assert meta.title == "Title of s1"
    assert meta.first_prompt == "Fix the bug"
    assert meta.last_prompt == "Now add a test"
    assert meta.cwd == "/work/app"
    assert meta.git_branch == "main"
    assert meta.user_prompts == 2
    assert meta.tool_calls == 2
    assert meta.models == ["claude-test"]
    assert meta.started_at < meta.ended_at
    assert meta.last_event == "reply"


def test_title_falls_back_to_first_prompt():
    objs = [o for o in make_lines("s1", "/w", ["Explain the build\nin detail"]) if o["type"] != "ai-title"]
    assert parse_meta(lines_of(objs)).title == "Explain the build"


def test_unfinished_turn_is_not_a_reply():
    meta = parse_meta(lines_of(make_lines("s1", "/w", ["Do it"], finish_turn=False)))
    assert meta.last_event == "tool_result"


def test_pending_question_is_recorded():
    objs = make_lines("s1", "/w", ["Do it"])
    objs.append({"type": "assistant", "sessionId": "s1", "timestamp": "2026-10-01T10:00:09.000Z",
                 "message": {"id": "q", "stop_reason": "tool_use", "content": [
                     {"type": "tool_use", "id": "t9", "name": "AskUserQuestion", "input": {}}]}})
    meta = parse_meta(lines_of(objs))
    assert (meta.last_event, meta.last_tool) == ("tool_call", "AskUserQuestion")


def test_broken_and_unknown_lines_are_tolerated():
    raw = lines_of(make_lines("s1", "/w", ["Hello"]))
    raw += [b'{"type": "something-new", "payload": [1, 2]}\n', b'{"truncated": \n', b"[1, 2]\n"]
    meta = parse_meta(raw)
    assert meta.user_prompts == 1
    assert meta.parse_errors == 2


def test_human_text_skips_generated_lines():
    def user(content, **extra):
        return {"type": "user", "message": {"content": content}, **extra}

    assert human_text(user("hello")) == "hello"
    assert human_text(user("<system-reminder>x</system-reminder>\nreal question")) == "real question"
    assert human_text(user("<command-name>/clear</command-name>")) is None
    assert human_text(user("hello", isSidechain=True)) is None
    assert human_text(user("hello", isMeta=True)) is None
    assert human_text(user([{"type": "tool_result", "content": "x"}])) is None
    assert human_text(user([{"type": "image"}])) == "[attachment]"


def test_sidechain_lines_do_not_move_the_session():
    objs = make_lines("s1", "/work/app", ["Go"])
    objs.insert(0, {"type": "assistant", "isSidechain": True, "cwd": "/somewhere/else",
                    "sessionId": "s1", "message": {"id": "x", "content": []}})
    assert parse_meta(lines_of(objs)).cwd == "/work/app"
    assert first_cwd(lines_of(objs)) == "/work/app"


def test_turns_merge_blocks_and_attach_tool_results():
    turns = list(iter_turns(lines_of(make_lines("s1", "/w", ["First", "Second"]))))
    assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"]
    tool, text = turns[1]["parts"]
    assert tool["name"] == "Bash" and tool["result"] == "file.txt"
    assert json.loads(tool["input"]) == {"command": "ls"}
    assert text == {"kind": "text", "text": "Answer 0"}
