"""Tests for the workflow action pin check. The tag lookup is faked, so no network is used."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "cmd" / "action-pins" / "check_action_pins.py"

spec = importlib.util.spec_from_file_location("check_action_pins", MODULE)
pins = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pins)


GOOD = "e797f83bcb11b83ae66e0230d6156d7c80228e7c"
# The mistyped pin from #5: it looks like GOOD, but no such commit exists.
TYPO = "e79783bcb11b83ae66e023c6d615c7c802286e7c"
TAGS = {
    ("actions/setup-python", "v6.0.0"): GOOD,
    ("github/codeql-action", "v3.29.0"): "1" * 40,
}


def fake_resolve(repository, tag):
    return TAGS.get((repository, tag))


def workflow(tmp_path, *steps):
    path = tmp_path / "ci.yml"
    path.write_text("jobs:\n  test:\n    steps:\n" + "".join(f"      {step}\n" for step in steps), encoding="utf-8")
    return path


def test_pin_matching_its_tag_passes(tmp_path):
    path = workflow(tmp_path, f"- uses: actions/setup-python@{GOOD} # v6.0.0")
    assert pins.check_file(path, fake_resolve) == []


def test_mistyped_sha_is_reported(tmp_path):
    path = workflow(tmp_path, f"- uses: actions/setup-python@{TYPO} # v6.0.0")
    [problem] = pins.check_file(path, fake_resolve)
    assert f"{path}:4:" in problem
    assert f"pins {TYPO}, but v6.0.0 is {GOOD}" in problem


def test_unknown_tag_is_reported(tmp_path):
    path = workflow(tmp_path, f"- uses: actions/setup-python@{GOOD} # v9.9.9")
    [problem] = pins.check_file(path, fake_resolve)
    assert "tag v9.9.9 does not exist in actions/setup-python" in problem


def test_branch_or_tag_ref_is_reported(tmp_path):
    path = workflow(tmp_path, "- uses: actions/setup-python@v6")
    [problem] = pins.check_file(path, fake_resolve)
    assert "pinned to 'v6', not a full commit SHA" in problem


def test_pin_without_version_comment_is_reported(tmp_path):
    path = workflow(tmp_path, f"- uses: actions/setup-python@{GOOD}")
    [problem] = pins.check_file(path, fake_resolve)
    assert "has no '# <tag>' comment" in problem


def test_action_in_a_subdirectory_resolves_against_its_repository(tmp_path):
    path = workflow(tmp_path, f"- uses: github/codeql-action/init@{'1' * 40} # v3.29.0")
    assert pins.check_file(path, fake_resolve) == []


def test_uses_without_list_dash_is_checked(tmp_path):
    path = workflow(tmp_path, "- name: Set up Python", f"  uses: actions/setup-python@{TYPO} # v6.0.0")
    assert len(pins.check_file(path, fake_resolve)) == 1


def test_local_and_docker_references_are_skipped(tmp_path):
    path = workflow(
        tmp_path,
        "- uses: ./.github/actions/setup",
        "- uses: docker://alpine:3.20",
    )
    assert pins.check_file(path, fake_resolve) == []


def test_tag_commit_prefers_the_peeled_commit_of_an_annotated_tag():
    output = f"{'a' * 40}\trefs/tags/v1.0.0\n{'b' * 40}\trefs/tags/v1.0.0^{{}}\n"
    assert pins.tag_commit(output, "v1.0.0") == "b" * 40


def test_tag_commit_of_a_lightweight_tag_and_a_missing_tag():
    output = f"{'a' * 40}\trefs/tags/v1.0.0\n"
    assert pins.tag_commit(output, "v1.0.0") == "a" * 40
    assert pins.tag_commit(output, "v2.0.0") is None


def test_main_exit_codes(tmp_path, capsys):
    good = workflow(tmp_path, f"- uses: actions/setup-python@{GOOD} # v6.0.0")
    assert pins.main([str(good)], resolve=fake_resolve) == 0

    good.write_text(good.read_text().replace(GOOD, TYPO))
    assert pins.main([str(tmp_path)], resolve=fake_resolve) == 1
    assert TYPO in capsys.readouterr().out

    def unreachable(repository, tag):
        raise pins.ResolveError("git ls-remote https://github.com/actions/setup-python failed")

    assert pins.main([str(good)], resolve=unreachable) == 2


def test_each_tag_is_resolved_once(tmp_path):
    calls = []

    def counting(repository, tag):
        calls.append((repository, tag))
        return GOOD

    workflow(tmp_path, *[f"- uses: actions/setup-python@{GOOD} # v6.0.0"] * 3)
    assert pins.main([str(tmp_path)], resolve=counting) == 0
    assert calls == [("actions/setup-python", "v6.0.0")]
