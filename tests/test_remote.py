import pytest

from claude_hub.remote import is_within, norm_path, normalize_remote


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/Robing98/claude-hub.git", "github.com/robing98/claude-hub"),
    ("git@github.com:Robing98/claude-hub.git", "github.com/robing98/claude-hub"),
    ("ssh://git@git.example.de:2222/group/sub/plugin.git", "git.example.de/group/sub/plugin"),
    ("https://user:secret-token@gitlab.com/group/project", "gitlab.com/group/project"),
    ("https://github.com/owner/name/", "github.com/owner/name"),
    ("C:\\repos\\thing", None),
    ("C:/repos/thing", None),
    ("/srv/git/thing.git", None),
    ("../sibling", None),
    ("file:///srv/git/thing.git", None),
    ("", None),
    (None, None),
])
def test_normalize_remote(url, expected):
    assert normalize_remote(url) == expected


def test_same_repository_over_https_and_ssh_is_one_identity():
    assert normalize_remote("https://github.com/A/B.git") == normalize_remote("git@github.com:a/b")


def test_credentials_never_survive_normalization():
    assert "secret" not in normalize_remote("https://bot:secret@host.example/a/b.git")


def test_norm_path_and_is_within():
    assert norm_path("D:\\Dev\\Orbis\\") == "d:/dev/orbis"
    assert norm_path("/home/Robin/x/") == "/home/Robin/x"
    assert is_within("D:\\dev\\orbis\\.claude\\worktrees\\a", "d:/dev/orbis")
    assert is_within("/a/b", "/a/b")
    assert not is_within("/a/bc", "/a/b")
    assert not is_within(None, "/a")
