"""Shared defaults for generated Git commit messages."""

DEFAULT_COMMIT_MESSAGE_PROMPT = (
    "Write a Git commit message based only on the staged diff provided. "
    "Describe the actual changes and their purpose when evident. "
    "Do not invent tests, issue numbers, motivation, or changes absent from the diff. "
    "Use a single-line summary, preferably at most 72 characters, without a trailing period. "
    "Include a useful description explaining the main changes. "
    "Use type(scope): summary; omit scope if unclear. "
    "Choose an appropriate type from feat, fix, docs, refactor, perf, test, "
    "build, ci, chore, revert. "
    "Keep type/scope in English. Mark breaking changes only when proven by the diff. "
    "Use concise bullet points in the description."
)

COMMIT_MESSAGE_LANGUAGES = {"ru": "Russian", "en": "English"}
