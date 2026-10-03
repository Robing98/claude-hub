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


def test_work_dirs_count_the_folders_a_session_touched():
    def tool(name, **data):
        return {"type": "assistant", "cwd": "D:\\", "sessionId": "s",
                "message": {"id": name, "content": [{"type": "tool_use", "id": name, "name": "X", "input": data}]}}

    objs = [
        tool("a", file_path="D:\\dev\\orbis\\src\\World.cs"),
        tool("b", file_path="D:\\dev\\orbis\\src\\Player.cs"),
        tool("c", path="D:\\dev\\orbis"),
        tool("d", command='cd /d "D:\\dev\\orbis\\tools" && dotnet build'),
        tool("e", command="cd /home/robin/code/app && make; cd relative/path"),
        tool("f", file_path="relative/file.txt"),
        tool("g", notebook_path="/srv/nb/analysis.ipynb"),
    ]
    dirs = parse_meta(lines_of(objs)).work_dirs
    assert dirs["D:\\dev\\orbis\\src"] == 2
    assert dirs["D:\\dev\\orbis"] == 1
    assert dirs["D:\\dev\\orbis\\tools"] == 1
    assert dirs["/home/robin/code/app"] == 1
    assert dirs["/srv/nb"] == 1
    assert dirs["D:\\"] == len(objs)
    assert not any("relative" in key for key in dirs)


def test_usage_counts_each_message_once_per_day_and_model():
    def reply(message_id, stamp, usage, model="claude-opus-5-5", **extra):
        return {"type": "assistant", "sessionId": "s", "timestamp": stamp, **extra,
                "message": {"id": message_id, "model": model, "usage": usage,
                            "content": [{"type": "text", "text": "x"}]}}

    first = {"input_tokens": 2, "output_tokens": 5, "cache_creation_input_tokens": 100,
             "cache_read_input_tokens": 1000,
             "cache_creation": {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 60}}
    meta = parse_meta(lines_of([
        # One message, written as two lines. The last line carries the final output count.
        reply("m1", "2026-10-01T23:59:00.000Z", first),
        reply("m1", "2026-10-01T23:59:01.000Z", {**first, "output_tokens": 50}),
        # An older shape without the split counts as a 5-minute cache write.
        reply("m2", "2026-10-02T00:01:00.000Z", {"input_tokens": 1, "output_tokens": 7,
                                                 "cache_creation_input_tokens": 9}),
        # A subagent line inside the session file counts as well.
        reply("m3", "2026-10-02T00:02:00.000Z", {"input_tokens": 3, "output_tokens": 4},
              model="claude-haiku-4-5", isSidechain=True),
        # Lines that Claude Code writes itself carry no usage worth counting.
        reply("m4", "2026-10-02T00:03:00.000Z", {"input_tokens": 0, "output_tokens": 0},
              model="<synthetic>"),
        reply("m5", "2026-10-02T00:04:00.000Z", "broken"),
    ]))
    assert meta.usage == {
        "2026-10-01": {"claude-opus-5-5": [1, 2, 50, 40, 60, 1000]},
        "2026-10-02": {"claude-opus-5-5": [1, 1, 7, 9, 0, 0], "claude-haiku-4-5": [1, 3, 4, 0, 0, 0]},
    }
