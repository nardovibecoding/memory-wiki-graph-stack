#!/usr/bin/env python3
# Copyright (c) 2026 Nardo. AGPL-3.0 — see LICENSE
"""
Batch promote: all lessons in 1 call per model (4 parallel) + 1 Opus batch for rule text.
Total: 5 API calls regardless of how many lessons.

Committee (4 voters, each sees ALL lessons at once):
  - MiniMax M2.7     : outsider perspective
  - Kimi             : secondary evaluator
  - Gemini 2.5 Flash : secondary evaluator
  - Claude Sonnet    : overlap checker (subscription, unset ANTHROPIC_API_KEY)

Arbiter (1): Claude Opus batch (subscription) — writes rule text for all promoted lessons.

Usage:
    python3 promote_batch.py               # dry run, show report
    python3 promote_batch.py --apply       # update frontmatter (status: promoted)
    python3 promote_batch.py --write-rules # append rules to build_system_prompt.py
    python3 promote_batch.py --json        # machine-readable output
    python3 promote_batch.py --limit 10    # cap candidates
"""

import argparse
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

# ── Config (shared with promote_lessons.py) ───────────────────────────────────

_CONFIG_PATH = Path.home() / ".config" / "llm-wiki-stack" / "config.yaml"
_LINT_SCRIPTS = Path(__file__).parent.parent / "lint" / "scripts"
if str(_LINT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_LINT_SCRIPTS))
from config_loader import write_md_file  # noqa: E402


def _load_config() -> dict:
    config = {}
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH) as f:
            config = yaml.safe_load(f) or {}

    def expand(p):
        return Path(os.path.expanduser(str(p))) if p else None

    promote = config.get("promote", {})
    return {
        "wiki_dir": expand(config.get("wiki_dir", "~/NardoWorld")),
        "llm_client_path": expand(promote.get("llm_client_path", "~/telegram-claude-bot")),
        "system_prompt_script": expand(promote.get("system_prompt_script", "~/.claude/scripts/build_system_prompt.py")),
        "min_votes": int(promote.get("min_votes", 3)),
    }


CFG = _load_config()


def _load_llm_client():
    llm_path = CFG["llm_client_path"] / "llm_client.py"
    if not llm_path.exists():
        print(f"ERROR: llm_client.py not found at {llm_path}", file=sys.stderr)
        sys.exit(1)
    bot_dir = str(CFG["llm_client_path"])
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location("llm_client", llm_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_llm = _load_llm_client()

# ── Load current ruleset ──────────────────────────────────────────────────────

def load_current_ruleset() -> str:
    script = CFG["system_prompt_script"]
    if not script or not script.exists():
        return "(no current ruleset found)"
    src = script.read_text()
    rules = []
    for tag in ("critical_rules", "behavioral_rules"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", src, re.DOTALL)
        if m:
            rules.append(f"[{tag}]\n{m.group(1).strip()}")
    return "\n\n".join(rules) if rules else "(no rules found)"


# ── Load pending lessons ──────────────────────────────────────────────────────

def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, m.group(2).strip()


def load_pending_lessons() -> list[dict]:
    lessons_dir = CFG["wiki_dir"] / "lessons"
    if not lessons_dir.exists():
        print(f"ERROR: lessons dir not found: {lessons_dir}", file=sys.stderr)
        sys.exit(1)
    results = []
    for path in sorted(lessons_dir.glob("*.md")):
        text = path.read_text()
        fm, body = parse_frontmatter(text)
        if fm.get("status") == "pending":
            results.append({
                "path": path,
                "title": fm.get("title", path.stem),
                "tags": fm.get("tags", ""),
                "frontmatter": fm,
                "body": body,
                "raw": text,
            })
    return results


# ── Batch prompts ─────────────────────────────────────────────────────────────

def build_lesson_list(lessons: list[dict]) -> str:
    lines = []
    for i, l in enumerate(lessons, 1):
        lines.append(f"{i}. [{l['title']}]\n{l['body'][:400]}")
    return "\n\n".join(lines)


_BATCH_VOTER_SYSTEM = """You are evaluating a list of behavioral lessons for an AI assistant.
Decide whether each should become a permanent rule in the system prompt.

Current rules already in the system prompt (do NOT re-promote these):
{ruleset}

For each lesson numbered below, respond with EXACTLY this format (one line per lesson):
1: PROMOTE — reason
2: REJECT — reason
3: MERGE — overlaps with rule X

Rules:
- PROMOTE: genuinely useful, not already covered, broadly applicable
- REJECT: too niche, already covered, or not actionable as a rule
- MERGE: overlaps with an existing rule (name which one)

Output ONLY the numbered decisions, one per line. No preamble, no extra text."""

_BATCH_SONNET_SYSTEM = """You are the overlap checker for a list of AI behavioral lessons.
Check each lesson against the existing system prompt rules — flag any that duplicate or significantly overlap.

Current rules:
{ruleset}

For each lesson numbered below, respond with EXACTLY this format (one line per lesson):
1: PROMOTE — reason
2: REJECT — already covered by rule X
3: MERGE — same as rule Y

Output ONLY the numbered decisions, one per line. No preamble, no extra text."""

_BATCH_OPUS_SYSTEM = """You are writing final rule text for behavioral lessons that passed committee vote.
Write a single concise rule (max 15 words) for each promoted lesson.
Match the style and tone of the existing rules listed below.

Current rules:
{ruleset}

For each lesson numbered below, respond with EXACTLY this format (one line per lesson):
1: Always batch LLM calls; send all items in one prompt per model
2: Diagnose root cause after 2 identical errors before retrying

Output ONLY the numbered rules, one per line. No preamble, no extra text."""


# ── Parse batch response ──────────────────────────────────────────────────────

def parse_batch_votes(text: str, n: int) -> dict[int, tuple[str, str]]:
    """Parse 'N: VOTE — reason' lines. Handles angle brackets, markdown bold, en/em dash variants."""
    results = {}
    for line in text.splitlines():
        line = line.strip().lstrip("*<>").strip()
        # Strip markdown bold around number
        line = re.sub(r"^\*\*(\d+)\*\*", r"\1", line)
        m = re.match(r"(\d+)[>).:]\s*(PROMOTE|REJECT|MERGE)\s*[—\-–:]\s*(.*)", line, re.IGNORECASE)
        if m:
            idx = int(m.group(1))
            vote = m.group(2).upper()
            reason = m.group(3).strip()
            if 1 <= idx <= n:
                results[idx] = (vote, reason)
    return results


def parse_batch_rules(text: str, n: int) -> dict[int, str]:
    """Parse 'N: rule text' lines. Handles angle brackets and markdown."""
    results = {}
    for line in text.splitlines():
        line = line.strip().lstrip("*<>").strip()
        line = re.sub(r"^\*\*(\d+)\*\*", r"\1", line)
        m = re.match(r"(\d+)[>).:]\s*(.*)", line)
        if m:
            idx = int(m.group(1))
            rule = m.group(2).strip().strip("*")
            # Skip if rule looks like a VOTE line (contains PROMOTE/REJECT)
            if rule and not re.match(r"(PROMOTE|REJECT|MERGE)", rule, re.IGNORECASE):
                if 1 <= idx <= n:
                    results[idx] = rule
    return results


# ── Claude calls via subscription (claude --print, no API key needed) ─────────

def call_claude_print(system: str, user: str, model: str = "sonnet", timeout: int = 120) -> tuple[str, str]:
    """Call claude --print via subprocess with ANTHROPIC_API_KEY unset (uses subscription auth)."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    prompt = f"{system}\n\n---\n\n{user}"
    model_flag = ["--model", "claude-opus-4-6"] if model == "opus" else []
    try:
        result = subprocess.run(
            ["claude", "--print", *model_flag],
            input=prompt,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return "", result.stderr[:500] or f"exit code {result.returncode}"
        return result.stdout.strip(), ""
    except subprocess.TimeoutExpired:
        return "", "Request timed out."
    except Exception as e:
        return "", str(e)


# ── Main batch committee ──────────────────────────────────────────────────────

async def run_batch_committee(lessons: list[dict], ruleset: str, debug: bool = False) -> list[dict]:
    """
    4 parallel batch calls (one per model, all lessons at once).
    Then 1 Opus batch call for rule text on promoted lessons.
    Total: 5 API calls.
    """
    n = len(lessons)
    lesson_list = build_lesson_list(lessons)
    voter_sys = _BATCH_VOTER_SYSTEM.format(ruleset=ruleset)
    sonnet_sys = _BATCH_SONNET_SYSTEM.format(ruleset=ruleset)

    print(f"Sending {n} lessons to 4 voters in parallel...", flush=True)

    loop = asyncio.get_event_loop()

    # 4 parallel batch calls
    tasks = {
        "minimax": loop.run_in_executor(None, _llm.chat_completion,
            [{"role": "user", "content": lesson_list}], 4000, 90, voter_sys),
        "kimi": loop.run_in_executor(None, _llm._call_single_model,
            "kimi", [{"role": "system", "content": voter_sys}, {"role": "user", "content": lesson_list}], 4000, 90),
        "gemini": loop.run_in_executor(None, _llm._call_single_model,
            "gemini", [{"role": "system", "content": voter_sys}, {"role": "user", "content": lesson_list}], 4000, 90),
        "sonnet": loop.run_in_executor(None, call_claude_print, sonnet_sys, lesson_list, "sonnet", 120),
    }

    raw_results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    model_keys = list(tasks.keys())

    # Parse votes per model; retry failed models once
    model_votes: dict[str, dict[int, tuple[str, str]]] = {}
    failed_models: list[str] = []

    for key, raw in zip(model_keys, raw_results):
        if isinstance(raw, BaseException):
            err = str(raw)
            print(f"  {key}: ERROR — {err}", flush=True)
            model_votes[key] = {}
            failed_models.append(key)
            text = f"⚠️ {err}"
        elif isinstance(raw, tuple):
            text = raw[0] if raw[0] else ""
            if not text and raw[1]:
                print(f"  {key}: FAILED — {raw[1]}", flush=True)
                model_votes[key] = {}
                failed_models.append(key)
            else:
                model_votes[key] = parse_batch_votes(text, n)
        else:
            text = str(raw)
            model_votes[key] = parse_batch_votes(text, n)
        parsed = len(model_votes[key])
        # Also retry if parsed < 50% of expected (likely truncated or failed silently)
        if parsed < n // 2 and key not in failed_models:
            failed_models.append(key)
        print(f"  {key}: parsed {parsed}/{n} votes", flush=True)
        if debug:
            print(f"  [{key} raw]\n{text[:600]}\n", flush=True)

    # Retry failed/truncated models once
    if failed_models:
        print(f"\nRetrying {len(failed_models)} failed model(s): {failed_models}...", flush=True)
        retry_tasks = {}
        for key in failed_models:
            if key == "minimax":
                retry_tasks[key] = loop.run_in_executor(None, _llm.chat_completion,
                    [{"role": "user", "content": lesson_list}], 4000, 90, voter_sys)
            elif key in ("kimi", "gemini"):
                retry_tasks[key] = loop.run_in_executor(None, _llm._call_single_model,
                    key, [{"role": "system", "content": voter_sys}, {"role": "user", "content": lesson_list}], 4000, 90)
            elif key == "sonnet":
                retry_tasks[key] = loop.run_in_executor(None, call_claude_print, sonnet_sys, lesson_list, "sonnet", 120)
        retry_results = await asyncio.gather(*retry_tasks.values(), return_exceptions=True)
        for key, raw in zip(retry_tasks.keys(), retry_results):
            if isinstance(raw, BaseException):
                print(f"  {key}: retry failed — {raw}", flush=True)
            elif isinstance(raw, tuple):
                text = raw[0] if raw[0] else ""
                model_votes[key] = parse_batch_votes(text, n)
                print(f"  {key}: retry parsed {len(model_votes[key])}/{n}", flush=True)
            else:
                model_votes[key] = parse_batch_votes(str(raw), n)
                print(f"  {key}: retry parsed {len(model_votes[key])}/{n}", flush=True)

    # Dynamic min_votes: require majority of models that actually responded
    responded = sum(1 for v in model_votes.values() if len(v) >= n // 2)
    effective_min = max(2, responded - 1)  # need all-but-one of responders, min 2
    if responded < len(model_keys):
        print(f"\n⚠️  Only {responded}/{len(model_keys)} models responded — using min_votes={effective_min}", flush=True)

    # Tally votes per lesson
    promoted_indices = []
    lesson_results = []
    for i, lesson in enumerate(lessons, 1):
        votes = {}
        reasons = {}
        for model in model_keys:
            v, r = model_votes[model].get(i, ("REJECT", "no response"))
            votes[model] = v
            reasons[model] = r
        # Only count votes from models that actually responded
        active_votes = {m: v for m, v in votes.items() if model_votes[m].get(i) is not None}
        promote_count = sum(1 for v in active_votes.values() if v == "PROMOTE")
        merge_count = sum(1 for v in active_votes.values() if v == "MERGE")
        majority = (promote_count + merge_count) >= effective_min
        if majority:
            promoted_indices.append(i)
        lesson_results.append({
            "index": i,
            "title": lesson["title"],
            "path": str(lesson["path"]),
            "votes": votes,
            "reasons": reasons,
            "promote_count": promote_count,
            "merge_count": merge_count,
            "decision": "PROMOTE" if majority else "REJECT",
            "rule_text": None,
        })

    # Opus batch: rule text for all promoted lessons in 1 call
    if promoted_indices:
        print(f"\n{len(promoted_indices)} promoted → sending to Opus for rule text...", flush=True)
        promoted_lessons = [lessons[i - 1] for i in promoted_indices]
        opus_list = build_lesson_list(promoted_lessons)
        opus_sys = _BATCH_OPUS_SYSTEM.format(ruleset=ruleset)
        opus_raw = call_claude_print(opus_sys, opus_list, model="opus", timeout=120)
        opus_text = opus_raw[0] if opus_raw[0] else str(opus_raw[1])
        rule_map = parse_batch_rules(opus_text, len(promoted_lessons))

        # Map rule text back to lesson_results by promoted_indices order
        for pos, global_idx in enumerate(promoted_indices, 1):
            rule_text = rule_map.get(pos, "")
            # find in lesson_results
            for r in lesson_results:
                if r["index"] == global_idx:
                    r["rule_text"] = rule_text
                    break
        print(f"  Opus (subscription): parsed {len(rule_map)}/{len(promoted_indices)} rules", flush=True)

    return lesson_results


# ── Apply + write (shared with promote_lessons.py) ───────────────────────────

def apply_promotion(lesson_path: Path, lesson_raw: str, rule_text: str, rule_num: int | None = None) -> None:
    raw = re.sub(r"^status:.*$", "status: promoted", lesson_raw, flags=re.MULTILINE)
    if "rule_text:" not in raw:
        raw = raw.replace("\nupdated:", f"\nrule_text: \"{rule_text}\"\nupdated:", 1)
    if rule_num is not None and "system_prompt_rule:" not in raw:
        raw = raw.replace("\nupdated:", f"\nsystem_prompt_rule: {rule_num}\nupdated:", 1)
    write_md_file(lesson_path, raw)


def write_rules_to_prompt(rule_texts: list[str]) -> list[int]:
    from datetime import datetime
    script = CFG["system_prompt_script"]
    if not script or not script.exists():
        raise FileNotFoundError(f"system_prompt_script not found: {script}")
    src = script.read_text()
    # Timestamped backup (keeps history, allows revert)
    ts = datetime.now().strftime("%Y-%m-%dT%H%M")
    bak_dir = script.parent / "backups"
    bak_dir.mkdir(exist_ok=True)
    bak = bak_dir / f"{script.stem}.{ts}.bak.py"
    bak.write_text(src)
    print(f"  Backup: {bak}")
    block_m = re.search(r"<behavioral_rules>(.*?)</behavioral_rules>", src, re.DOTALL)
    if not block_m:
        raise ValueError("No <behavioral_rules> block found")
    block = block_m.group(1)
    nums = [int(m) for m in re.findall(r"^(\d+)\.", block, re.MULTILINE)]
    next_num = (max(nums) + 1) if nums else 1
    new_lines = []
    assigned = []
    for i, rule in enumerate(rule_texts):
        n = next_num + i
        new_lines.append(f"{n}. {rule}")
        assigned.append(n)
    insert = "\n".join(new_lines) + "\n"
    src = src.replace("</behavioral_rules>", insert + "</behavioral_rules>")
    # Update version stamp in file header
    version_line = f"# version: {ts} ({len(rule_texts)} rules added, total={next_num + len(rule_texts) - 1})"
    if re.search(r"^# version:.*$", src, re.MULTILINE):
        src = re.sub(r"^# version:.*$", version_line, src, flags=re.MULTILINE)
    else:
        src = src.replace('"""Generate', f"{version_line}\n\"\"\"Generate", 1)
    script.write_text(src)
    return assigned


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Batch promote lessons via AI committee (5 API calls total)")
    parser.add_argument("--apply", action="store_true", help="Update lesson frontmatter (status: promoted)")
    parser.add_argument("--write-rules", action="store_true", help="Append promoted rules to build_system_prompt.py")
    parser.add_argument("--limit", type=int, default=0, help="Max lessons to process (0=all)")
    parser.add_argument("--json", action="store_true", dest="json_out", help="JSON output")
    parser.add_argument("--debug", action="store_true", help="Print raw model outputs")
    args = parser.parse_args()

    ruleset = load_current_ruleset()
    candidates = load_pending_lessons()

    if not candidates:
        print("No pending lessons found.")
        return

    if args.limit:
        candidates = candidates[: args.limit]

    print(f"Loaded {len(candidates)} pending lessons")
    print(f"Current ruleset: {len(ruleset.splitlines())} lines\n")

    results = await run_batch_committee(candidates, ruleset, debug=args.debug)

    promoted = [r for r in results if r["decision"] == "PROMOTE"]
    rejected = [r for r in results if r["decision"] == "REJECT"]

    if args.json_out:
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"\n{'='*60}")
    print(f"RESULTS: {len(promoted)} PROMOTE / {len(rejected)} REJECT (min_votes={CFG['min_votes']}/4)\n")

    for r in promoted:
        votes_str = " | ".join(f"{k}:{v}" for k, v in r["votes"].items())
        print(f"  PROMOTE: {r['title']}")
        print(f"    Rule:  {r['rule_text'] or '(no rule text)'}")
        print(f"    Votes: {votes_str}")
        for model, reason in r["reasons"].items():
            print(f"    {model}: {reason}")
        print()

    if rejected:
        print(f"  REJECTED ({len(rejected)}):")
        for r in rejected:
            votes_str = ", ".join(f"{k}:{v}" for k, v in r["votes"].items())
            print(f"    - {r['title']} ({votes_str})")

    if args.apply or args.write_rules:
        candidate_map = {str(l["path"]): l for l in candidates}

        rule_num_map: dict[str, int] = {}
        if args.write_rules:
            rule_texts = [r["rule_text"] for r in promoted if r["rule_text"]]
            if rule_texts:
                print(f"\nWriting {len(rule_texts)} rules to build_system_prompt.py...")
                try:
                    assigned = write_rules_to_prompt(rule_texts)
                    for r, num in zip([p for p in promoted if p["rule_text"]], assigned):
                        rule_num_map[r["path"]] = num
                        print(f"  Rule {num}: {r['rule_text']}")
                except Exception as e:
                    print(f"  ERROR: {e}")

        if args.apply:
            print(f"\nApplying {len(promoted)} promotions...")
            for r in promoted:
                if not r["rule_text"]:
                    continue
                lesson = candidate_map.get(r["path"])
                if lesson:
                    rule_num = rule_num_map.get(r["path"])
                    apply_promotion(lesson["path"], lesson["raw"], r["rule_text"], rule_num)
                    print(f"  Promoted: {r['title']}")

    print(f"\nDone. 5 API calls used.")


if __name__ == "__main__":
    asyncio.run(main())
