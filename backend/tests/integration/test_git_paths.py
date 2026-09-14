import subprocess


def test_git_baseline_in_temporary_unicode_repository(tmp_path):
    workspace = tmp_path / "проект с пробелами"
    workspace.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(workspace), *args], check=True, capture_output=True
        ).stdout

    git("init")
    (workspace / "example.txt").write_text("baseline\n")
    git("add", "--", "example.txt")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "fixture baseline",
    )
    baseline = git("rev-parse", "HEAD")
    assert git("status", "--porcelain") == b""
    (workspace / "example.txt").write_text("user change\n")
    assert b"example.txt" in git("status", "--porcelain")
    assert git("rev-parse", "HEAD") == baseline
