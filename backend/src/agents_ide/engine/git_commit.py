"""Git baseline, verified tree, durable intent and conservative recovery.

No force checkout, branch reset, stash, amend or push. Hooks run through ordinary
git commit with a private index. The previously clean user index is synchronized
under its lock only after verifying that nobody staged anything during the call.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agents_ide.domain.common import content_hash
from agents_ide.engine.git_process import GitError as GitCommitError
from agents_ide.engine.git_process import optional_git, run_git
from agents_ide.security.workspace_read import read_workspace_file

INTENT_TRAILER = "Agents-Ide-Intent"
BASELINE_REF_TEMPLATE = "refs/agents-ide/run/{run_id}/baseline"
RUN_BRANCH_TEMPLATE = "agents-ide/run/{run_id}"
MAX_FILES = 10000
MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class FileEntry:
    path: str
    mode: str
    sha256: str
    size: int


@dataclass
class Baseline:
    head_sha: str
    branch: str | None
    tree: str
    manifest: tuple[FileEntry, ...]
    status: list[dict[str, str]] = field(default_factory=list)
    protected: dict[str, dict[str, Any]] = field(default_factory=dict)
    fingerprint: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    hooks: tuple[str, ...] = ()
    hooks_path: str | None = None
    signing_required: bool = False
    allowlist: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> Baseline:
        return cls(
            **{
                **body,
                "manifest": tuple(FileEntry(**x) for x in body["manifest"]),
                "hooks": tuple(body.get("hooks", [])),
                "allowlist": tuple(body.get("allowlist", [])),
            }
        )


@dataclass
class CommitIntent:
    operation_id: str
    run_id: str
    attempt_id: str
    parent_sha: str
    branch: str
    expected_tree: str
    allowlist: tuple[str, ...]
    message: str
    message_hash: str
    trailers: dict[str, str]
    policy: str
    hook_policy: str
    signing_required: bool
    manifest_hash: str = ""
    user_index_hash: str = ""
    baseline_ref: str = ""
    verification_id: str | None = None
    allow_untracked: bool = True

    def formatted_message(self) -> str:
        return (
            self.message.rstrip()
            + "\n\n"
            + "\n".join(f"{key}: {value}" for key, value in self.trailers.items())
            + "\n"
        )

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> CommitIntent:
        return cls(**{**body, "allowlist": tuple(body["allowlist"])})


@dataclass
class CommitResult:
    intent: CommitIntent
    sha: str | None
    actual_tree: str | None
    no_changes: bool
    stdout: str = ""
    stderr: str = ""
    recovered: bool = False
    tree_mismatch: bool = False


def _safe_path(path: str) -> str:
    if not isinstance(path, str) or not path or any(c in path for c in ("\0", ":", "\\")):
        raise GitCommitError("path_invalid", "Invalid relative Git path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts) or parts[0].startswith("-"):
        raise GitCommitError("path_invalid", "Invalid relative Git path")
    if any(
        p.lower() in {".git", ".env", "secrets", ".ssh", ".aws", "credentials"}
        or p.lower().startswith(".env.")
        or p.lower().endswith((".pem", ".key", ".p12"))
        for p in parts
    ):
        raise GitCommitError("path_protected", "Protected path cannot be committed")
    return path


def normalize_allowlist(paths: Iterable[str]) -> tuple[str, ...]:
    if not isinstance(paths, (list, tuple, set, frozenset)):
        raise GitCommitError("path_invalid", "Allowlist must be a list")
    result = tuple(sorted({_safe_path(p) for p in paths}))
    if not result or len(result) > 100:
        raise GitCommitError("path_invalid", "A bounded, nonempty allowlist is required")
    return result


def is_path_allowed(path: str, allowlist: Iterable[str]) -> bool:
    from agents_ide.engine.context_sources import _match_path

    try:
        _safe_path(path)
    except GitCommitError:
        return False
    return any(_match_path(path, pattern) for pattern in allowlist)


def read_head_sha(workspace: Path) -> str:
    return run_git(workspace, ["rev-parse", "--verify", "HEAD"]).decode().strip()


def has_head(workspace: Path) -> bool:
    return optional_git(workspace, ["rev-parse", "--verify", "HEAD"]) is not None


def read_branch(workspace: Path) -> str | None:
    return optional_git(workspace, ["symbolic-ref", "--short", "HEAD"])


def current_tree_sha(workspace: Path, ref: str = "HEAD") -> str:
    return run_git(workspace, ["rev-parse", "--verify", f"{ref}^{{tree}}"]).decode().strip()


def _git_path(workspace: Path, name: str) -> Path:
    return Path(
        run_git(workspace, ["rev-parse", "--path-format=absolute", "--git-path", name])
        .decode()
        .strip()
    )


def index_hash(workspace: Path) -> str:
    path = _git_path(workspace, "index")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"


def list_status(workspace: Path) -> list[dict[str, str]]:
    raw = run_git(
        workspace, ["status", "--porcelain=v2", "--no-renames", "--untracked-files=all", "-z"]
    )
    result = []
    for token in raw.decode("utf-8", "strict").split("\0"):
        if token.startswith("1 "):
            fields = token.split(" ", 8)
            result.append({"code": fields[1], "path": fields[8]})
        elif token.startswith("? "):
            result.append({"code": "??", "path": token[2:]})
        elif token.startswith("u "):
            raise GitCommitError("git_conflict", "Unmerged index is not supported")
    return result


def ls_tree(workspace: Path, ref: str = "HEAD") -> list[FileEntry]:
    rows = run_git(workspace, ["ls-tree", "-rz", ref]).decode("utf-8", "strict").split("\0")
    entries = []
    for row in filter(None, rows):
        metadata, path = row.split("\t", 1)
        mode, _, oid = metadata.split()
        entries.append(FileEntry(path, mode, oid, 0))
    return entries


def file_manifest(workspace: Path) -> dict[str, dict[str, Any]]:
    paths = set(
        filter(
            None,
            run_git(workspace, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
            .decode()
            .split("\0"),
        )
    )
    # Protect individual ignored files, but do not traverse ignored trees such
    # as node_modules, virtualenvs or local databases. They cannot enter staging.
    ignored = set(
        filter(
            None,
            run_git(
                workspace,
                ["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"],
            )
            .decode()
            .split("\0"),
        )
    )
    ignored = {path for path in ignored if not path.endswith("/")}
    paths.update(ignored)
    if len(paths) > MAX_FILES:
        raise GitCommitError("git_manifest_limit", "Workspace manifest exceeds file limit")
    result: dict[str, dict[str, Any]] = {}
    total = 0
    for path in sorted(paths):
        target = workspace / path
        if not target.exists() and not target.is_symlink():
            result[path] = {"missing": True}
            continue
        try:
            data = read_workspace_file(workspace, path, MAX_BYTES - total)
            total += len(data)
        except (ValueError, OSError) as exc:
            raise GitCommitError(
                "path_violation", "Cannot safely fingerprint workspace", details={"path": path}
            ) from exc
        mode = "100755" if os.name != "nt" and target.stat().st_mode & 0o111 else "100644"
        result[path] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mode": mode,
            "ignored": path in ignored,
        }
    return result


def manifest_hash(manifest: dict[str, dict[str, Any]]) -> str:
    # A deleted tracked path disappears from ls-files after the clean-index refresh.
    return content_hash({p: v for p, v in manifest.items() if not v.get("missing")})


def git_fingerprint(workspace: Path) -> dict[str, Any]:
    config = run_git(workspace, ["config", "--null", "--list", "--show-origin"])
    configured = optional_git(workspace, ["config", "--path", "--get", "core.hooksPath"])
    hook_root = Path(configured) if configured else _git_path(workspace, "hooks")
    if not hook_root.is_absolute():
        hook_root = workspace / hook_root
    hooks: dict[str, str] = {}
    if hook_root.is_dir():
        for item in sorted(hook_root.iterdir()):
            if item.name.endswith(".sample") or not item.is_file():
                continue
            if item.stat().st_size > 1024 * 1024:
                raise GitCommitError("git_hook_limit", "Hook exceeds fingerprint limit")
            hooks[item.name] = hashlib.sha256(item.read_bytes()).hexdigest()
    signing = (
        optional_git(workspace, ["config", "--type=bool", "--get", "commit.gpgsign"]) == "true"
    )
    return {
        "config_hash": hashlib.sha256(config).hexdigest(),
        "hooks": hooks,
        "hooks_path": str(hook_root.resolve()),
        "signing_required": signing,
    }


def git_policy_matches(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    # The full config hash is diagnostic only: worktrees share repository config,
    # and unrelated changes (e.g. extensions.worktreeConfig) must not block a run.
    # Explicit fields also keep baselines saved by older versions compatible.
    return all(
        current.get(key) == expected.get(key) for key in ("hooks", "hooks_path", "signing_required")
    )


def detect_hooks(workspace: Path) -> tuple[str, ...]:
    return tuple(git_fingerprint(workspace)["hooks"])


def detect_signing_required(workspace: Path) -> bool:
    return bool(git_fingerprint(workspace)["signing_required"])


def capture_baseline(
    workspace: Path,
    run_id: str,
    allowlist: Iterable[str],
    *,
    dirty_policy: str = "strict",
    allow_existing_changes: bool = False,
) -> Baseline:
    root = run_git(workspace, ["rev-parse", "--show-toplevel"]).decode().strip()
    if Path(root).resolve() != workspace.resolve():
        raise GitCommitError("workspace_conflict", "GitCommit requires the repository root")
    allowed = normalize_allowlist(allowlist)
    status = list_status(workspace)
    for item in status:
        code, path = item["code"], item["path"]
        if code != "??" and code[0] != ".":
            raise GitCommitError("git_index_dirty", "Existing staged changes block the run")
        if not allow_existing_changes and (
            (code != "??" and dirty_policy == "strict") or is_path_allowed(path, allowed)
        ):
            raise GitCommitError(
                "git_dirty", "Existing changes overlap the run", details={"path": path}
            )
    manifest = file_manifest(workspace)
    fingerprint = git_fingerprint(workspace)
    return Baseline(
        read_head_sha(workspace),
        read_branch(workspace),
        current_tree_sha(workspace),
        tuple(ls_tree(workspace)),
        status,
        {p: v for p, v in manifest.items() if v.get("ignored") or not is_path_allowed(p, allowed)},
        fingerprint,
        {"baseline": BASELINE_REF_TEMPLATE.format(run_id=run_id)},
        tuple(fingerprint["hooks"]),
        fingerprint["hooks_path"],
        fingerprint["signing_required"],
        allowed,
    )


def update_baseline_ref(workspace: Path, run_id: str, sha: str) -> None:
    ref = BASELINE_REF_TEMPLATE.format(run_id=run_id)
    existing = optional_git(workspace, ["rev-parse", "--verify", ref])
    if existing == sha:
        return
    if existing:
        raise GitCommitError("external_change_detected", "Baseline ref already points elsewhere")
    run_git(workspace, ["update-ref", ref, sha, "0" * len(sha)])


def ensure_run_branch(workspace: Path, run_id: str, parent_sha: str) -> str:
    branch = RUN_BRANCH_TEMPLATE.format(run_id=run_id)
    existing = optional_git(workspace, ["rev-parse", "--verify", f"refs/heads/{branch}"])
    if existing and existing != parent_sha:
        raise GitCommitError("external_change_detected", "Existing run branch has moved")
    if not existing:
        run_git(
            workspace, ["update-ref", f"refs/heads/{branch}", parent_sha, "0" * len(parent_sha)]
        )
    if read_branch(workspace) != branch:
        run_git(workspace, ["checkout", branch])
    return branch


def check_workspace(
    workspace: Path,
    baseline: Baseline,
    allowlist: tuple[str, ...],
    *,
    expected_head: str,
    branch: str,
) -> dict[str, dict[str, Any]]:
    if read_head_sha(workspace) != expected_head or read_branch(workspace) != branch:
        raise GitCommitError("external_change_detected", "Branch or HEAD changed externally")
    if not git_policy_matches(git_fingerprint(workspace), baseline.fingerprint):
        raise GitCommitError("external_change_detected", "Git hooks/signing changed after Start")
    for item in list_status(workspace):
        if item["code"] != "??" and item["code"][0] != ".":
            raise GitCommitError("git_index_dirty", "User staged changes after Start")
    manifest = file_manifest(workspace)
    # Fresh worktrees acquire ignored setup/test files (.env, pytest caches).
    # They cannot enter the index and were not user files present at Start.
    # Still protect every pre-existing ignored file and every nonignored path
    # outside the allowlist.
    protected = {
        p: v
        for p, v in manifest.items()
        if p in baseline.protected
        or (not v.get("ignored") and not is_path_allowed(p, baseline.allowlist or allowlist))
    }
    if protected != baseline.protected:
        raise GitCommitError("external_change_detected", "Files outside the allowlist changed")
    return manifest


def _build_temp_index(
    workspace: Path,
    parent: str,
    allowlist: tuple[str, ...],
    manifest: dict[str, dict[str, Any]],
    directory: Path,
    allow_untracked: bool = True,
) -> tuple[Path, str]:
    index = directory / "index"
    env = {"GIT_INDEX_FILE": str(index)}
    run_git(workspace, ["read-tree", parent], env=env)
    tracked = {entry.path: entry for entry in ls_tree(workspace, parent)}
    # A fresh private index has no user stat cache, assume-unchanged or
    # skip-worktree flags. Let one Git process compare tracked contents using
    # its clean/EOL rules instead of starting hash-object for every clean file.
    changed_tracked = set(
        run_git(
            workspace,
            [
                "diff",
                "--name-only",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "-z",
                parent,
                "--",
            ],
            env=env,
        )
        .decode("utf-8", "strict")
        .split("\0")
    )
    updates = bytearray()
    for path, item in manifest.items():
        if path not in tracked and not allow_untracked:
            continue
        if item.get("ignored") or not is_path_allowed(path, allowlist):
            continue
        if item.get("missing"):
            updates.extend(f"0 {'0' * len(parent)}\t{path}\0".encode())
            continue
        mode = tracked[path].mode if os.name == "nt" and path in tracked else item["mode"]
        if mode not in {"100644", "100755"}:
            raise GitCommitError("path_violation", "Symlinks/submodules cannot enter a commit")
        if path in tracked and path not in changed_tracked and mode == tracked[path].mode:
            continue
        data = read_workspace_file(workspace, path, MAX_BYTES)
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise GitCommitError("external_change_detected", "File changed while building tree")
        # Apply Git's declared clean/EOL rules. Filters are supervised and checked afterwards.
        oid = (
            run_git(workspace, ["hash-object", "-w", f"--path={path}", "--stdin"], data=data)
            .decode()
            .strip()
        )
        updates.extend(f"{mode} {oid}\t{path}\0".encode())
    if updates:
        run_git(workspace, ["update-index", "-z", "--index-info"], data=bytes(updates), env=env)
    return index, run_git(workspace, ["write-tree"], env=env).decode().strip()


def _commit_metadata(workspace: Path, sha: str) -> tuple[str, str, str]:
    raw = run_git(workspace, ["show", "-s", "--format=%P%x00%T%x00%B", sha]).decode()
    parent, tree, body = raw.split("\0", 2)
    return parent, tree, body


def find_intent_commit(
    workspace: Path, branch: str, parent_sha: str, tree_sha: str, intent_id: str
) -> str | None:
    head = optional_git(workspace, ["rev-parse", "--verify", f"refs/heads/{branch}"])
    if not head or read_branch(workspace) != branch:
        return None
    parent, tree, body = _commit_metadata(workspace, head)
    trailers = (
        run_git(workspace, ["interpret-trailers", "--parse"], data=body.encode())
        .decode()
        .splitlines()
    )
    matches = [line for line in trailers if line.startswith(f"{INTENT_TRAILER}:")]
    if parent == parent_sha and tree == tree_sha and matches == [f"{INTENT_TRAILER}: {intent_id}"]:
        return head
    return None


def _sync_clean_index(workspace: Path, intent: CommitIntent, sha: str) -> None:
    """Refresh only the proven unchanged clean index, atomically under index.lock."""
    index = _git_path(workspace, "index")
    lock = index.with_name(index.name + ".lock")
    try:
        with lock.open("xb") as guard:
            if index_hash(workspace) != intent.user_index_hash:
                raise GitCommitError("external_change_detected", "User index changed during commit")
            with tempfile.TemporaryDirectory(prefix="agents-ide-index-") as tmp:
                replacement = Path(tmp) / "index"
                run_git(workspace, ["read-tree", sha], env={"GIT_INDEX_FILE": str(replacement)})
                guard.write(replacement.read_bytes())
                guard.flush()
                os.fsync(guard.fileno())
        os.replace(lock, index)
    except FileExistsError:
        raise GitCommitError("external_change_detected", "User index is locked") from None
    finally:
        # Never remove a lock created by another owner.
        if "guard" in locals() and lock.exists():
            lock.unlink()


def execute(
    workspace: Path,
    baseline: Baseline,
    intent: CommitIntent,
    *,
    recover_only: bool = False,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    generate_message: Callable[[dict[str, Any]], str] | None = None,
) -> CommitResult:
    if recover_only:
        if (
            read_head_sha(workspace) == intent.parent_sha
            and current_tree_sha(workspace, intent.parent_sha) == intent.expected_tree
        ):
            manifest = check_workspace(
                workspace,
                baseline,
                intent.allowlist,
                expected_head=intent.parent_sha,
                branch=intent.branch,
            )
            if manifest_hash(manifest) != intent.manifest_hash:
                raise GitCommitError("external_change_detected", "Files changed after no_changes")
            return CommitResult(intent, None, intent.expected_tree, True, recovered=True)
        existing = find_intent_commit(
            workspace, intent.branch, intent.parent_sha, intent.expected_tree, intent.operation_id
        )
        if not existing:
            raise GitCommitError(
                "unknown_external_result", "Cannot prove the outcome of Git intent"
            )
        # A crash can occur before or after the clean-index refresh.
        if index_hash(workspace) == intent.user_index_hash:
            _sync_clean_index(workspace, intent, existing)
        elif any(e["code"] != "??" and e["code"][0] != "." for e in list_status(workspace)):
            raise GitCommitError("external_change_detected", "Index changed during recovery")
        if manifest_hash(file_manifest(workspace)) != intent.manifest_hash:
            raise GitCommitError("external_change_detected", "Working files changed after commit")
        if not git_policy_matches(git_fingerprint(workspace), baseline.fingerprint):
            raise GitCommitError("external_change_detected", "Git policy changed during recovery")
        return CommitResult(intent, existing, intent.expected_tree, False, recovered=True)
    manifest = check_workspace(
        workspace, baseline, intent.allowlist, expected_head=intent.parent_sha, branch=intent.branch
    )
    initial_index = index_hash(workspace)
    with tempfile.TemporaryDirectory(prefix="agents-ide-commit-") as tmp:
        index, tree = _build_temp_index(
            workspace,
            intent.parent_sha,
            intent.allowlist,
            manifest,
            Path(tmp),
            intent.allow_untracked,
        )
        if manifest_hash(file_manifest(workspace)) != manifest_hash(manifest):
            raise GitCommitError("external_change_detected", "Files changed during Git filtering")
        intent.expected_tree = tree
        intent.manifest_hash = manifest_hash(manifest)
        intent.user_index_hash = initial_index
        intent.baseline_ref = baseline.refs.get("baseline", "")
        if generate_message and tree != current_tree_sha(workspace, intent.parent_sha):
            intent.message = safe_generated_message(
                generate_message(staged_message_diff(workspace, index, intent.parent_sha))
            )
            if manifest_hash(file_manifest(workspace)) != intent.manifest_hash:
                raise GitCommitError(
                    "external_change_detected", "Files changed during message generation"
                )
        intent.message_hash = content_hash(intent.formatted_message())
        if on_event:
            on_event("git.commit_intent_saved", asdict(intent))
        if tree == current_tree_sha(workspace, intent.parent_sha):
            result = CommitResult(intent, None, tree, True)
            if on_event:
                on_event(
                    "git.no_changes",
                    {
                        "intent_id": intent.operation_id,
                        "parent_sha": intent.parent_sha,
                        "expected_tree": tree,
                    },
                )
            return result
        check_workspace(
            workspace,
            baseline,
            intent.allowlist,
            expected_head=intent.parent_sha,
            branch=intent.branch,
        )
        if index_hash(workspace) != initial_index:
            raise GitCommitError("external_change_detected", "Index changed before Git commit")
        output = run_git(
            workspace,
            ["commit", "--file=-", "--cleanup=verbatim"],
            data=intent.formatted_message().encode(),
            env={"GIT_INDEX_FILE": str(index)},
        )
        sha = read_head_sha(workspace)
        parent, actual, _ = _commit_metadata(workspace, sha)
        # Git show adds a newline; use parsed trailer for authoritative identity.
        valid = (
            parent == intent.parent_sha
            and actual == tree
            and find_intent_commit(
                workspace, intent.branch, intent.parent_sha, tree, intent.operation_id
            )
            == sha
        )
        if on_event:
            on_event(
                "git.commit_created",
                {
                    "intent_id": intent.operation_id,
                    "commit_sha": sha,
                    "parent_sha": parent,
                    "expected_tree": tree,
                    "actual_tree": actual,
                    "verified": valid,
                },
            )
        if not valid or manifest_hash(file_manifest(workspace)) != intent.manifest_hash:
            raise GitCommitError(
                "external_change_detected",
                "Hook changed the committed tree or working files",
                details={"commit_sha": sha, "actual_tree": actual, "expected_tree": tree},
            )
        if not git_policy_matches(git_fingerprint(workspace), baseline.fingerprint):
            raise GitCommitError(
                "external_change_detected", "Git hooks/signing changed during commit"
            )
        _sync_clean_index(workspace, intent, sha)
        return CommitResult(intent, sha, actual, False, stdout=output.decode("utf-8", "replace"))


def list_run_commits(workspace: Path, run_id: str, limit: int = 1000) -> list[dict[str, Any]]:
    baseline = BASELINE_REF_TEMPLATE.format(run_id=run_id)
    raw = (
        run_git(
            workspace,
            ["log", f"{baseline}..HEAD", f"-n{limit + 1}", "--format=%H%x00%P%x00%s", "-z"],
        )
        .decode()
        .split("\0")
    )
    result = []
    for i in range(0, len(raw) - 2, 3):
        result.append({"sha": raw[i], "parents": raw[i + 1].split(), "subject": raw[i + 2]})
    if len(result) > limit:
        raise GitCommitError("git_history_limit", "Run history exceeds the context limit")
    return result


def staged_message_diff(workspace: Path, index: Path, parent: str) -> dict[str, Any]:
    """Use the exact private staging index, omitting deleted file contents."""
    env = {"GIT_INDEX_FILE": str(index)}
    common = ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color"]
    patch = run_git(workspace, [*common, "--diff-filter=d", parent, "--"], env=env)
    deleted = run_git(
        workspace, [*common, "--diff-filter=D", "--name-only", "-z", parent, "--"], env=env
    )
    return {
        "staged_diff": patch.decode("utf-8", "replace"),
        "deleted_files": [name.decode("utf-8", "replace") for name in deleted.split(b"\0") if name],
    }


def _without_commit_thinking(text: str) -> str:
    # Keep the provider's reasoning in the raw response artifact, never in Git.
    parts: list[str] = []
    depth, start = 0, 0
    for tag in re.finditer(r"<(/?)think\s*>", text, re.IGNORECASE):
        if not tag[1]:
            if depth == 0:
                parts.append(text[start : tag.start()])
            depth += 1
        else:
            if depth == 0:
                return ""  # An unmatched tag is not a complete final answer.
            depth -= 1
            if depth == 0:
                start = tag.end()
    return "" if depth else "".join([*parts, text[start:]]).strip()


def safe_generated_message(text: str) -> str:
    value = _without_commit_thinking(text.replace("\r\n", "\n").strip())
    if value.startswith("```") and value.endswith("```"):
        value = "\n".join(value.splitlines()[1:-1]).strip()
    if (
        not value
        or len(value) > 8192
        or any(ord(char) < 32 and char not in "\n\t" for char in value)
    ):
        raise GitCommitError(
            "commit_message_invalid", "LLM returned an empty or invalid commit message"
        )
    if re.search(rf"(?im)^\s*{INTENT_TRAILER}\s*:", value):
        raise GitCommitError("commit_message_invalid", "LLM returned a reserved commit trailer")
    return value


def safe_message(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())[:512] or "Agents IDE update"
