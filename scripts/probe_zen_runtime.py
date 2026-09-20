"""Explicit free Zen acceptance with real OpenCode/Runner and a synthetic HTTP LLM.

Uses only synthetic prompts in a disposable workspace. The native provider is pinned
to one verified zero-price Zen model including auxiliary calls. Account credentials
are not copied into its isolated environment. No paid model or automatic substitution.
"""

import argparse
import json
import subprocess
from pathlib import Path

from probe_native_runtime import probe


def free_model(executable: Path, model: str) -> dict:
    if not model.startswith("opencode/"):
        raise ValueError("Select an opencode/ model")
    result = subprocess.run(
        [str(executable), "models", "opencode", "--verbose"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    marker = model + "\n"
    if marker not in result.stdout:
        raise ValueError("Model is absent from the installed catalog")
    metadata, _ = json.JSONDecoder().raw_decode(result.stdout.split(marker, 1)[1].lstrip())
    cost = metadata["cost"]
    if any(cost.get(k) != 0 for k in ("input", "output")) or any(
        cost.get("cache", {}).get(k) != 0 for k in ("read", "write")
    ):
        raise ValueError("The selected model does not have verified zero prices")
    if (
        metadata["api"]["url"] != "https://opencode.ai/zen/v1"
        or metadata["api"]["npm"] != "@ai-sdk/openai-compatible"
        or not metadata["capabilities"]["toolcall"]
    ):
        raise ValueError("Select a free Zen Chat Completions model with tool support")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--controls", action="store_true")
    args = parser.parse_args()
    if args.output.exists() or not args.opencode.is_absolute() or not args.opencode.is_file():
        parser.error("Use an existing absolute executable and a new report path")
    metadata = free_model(args.opencode, args.model)
    report = probe(args.opencode, zen_model=args.model, controls=args.controls)
    report.update(model=args.model, catalog_cost=metadata["cost"], account_credentials_used=False)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return (
        0
        if (
            report["all_processes_stopped"]
            and not report["provider_failures"]
            and (args.controls or report["agent_group_fallback"])
            and report["distinct_sessions"] >= 3
            and (
                (
                    not args.controls
                    and report["pipeline_state"] == report["single_agent_state"] == "completed"
                    and report["native_structured_results"] == 3
                    and report["llm_group_fallback"]
                )
                or (
                    args.controls
                    and report["native_structured_results"] == 2
                    and len(report["controls"]) == 2
                    and all(
                        c.get("resumed_state") == "completed"
                        and not c["controller_errors"]
                        and c.get("sessions") == 2
                        and c.get("session_reused") == (c["command"] == "pause")
                        for c in report["controls"]
                    )
                )
            )
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
