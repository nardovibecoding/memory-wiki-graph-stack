#!/usr/bin/env python3
# Copyright (c) 2026 Nardo. AGPL-3.0 — see LICENSE
"""
Promote pending lessons to system prompt rules via AI committee vote.

Committee (4 voters):
  - MiniMax M2.7     : primary evaluator (outsider perspective)
  - Kimi             : secondary evaluator
  - Gemini 2.5 Flash : secondary evaluator
  - Claude Sonnet    : overlap checker (flags redundancy with existing rules)

Arbiter (1): Claude Opus — writes final rule text when majority vote PROMOTE

Usage:
    python3 promote_lessons.py               # dry run, show report
    python3 promote_lessons.py --apply       # update lesson frontmatter (status: promoted)
    python3 promote_lessons.py --limit 10    # cap candidates to process
    python3 promote_lessons.py --json        # machine-readable output
"""

import argparse
import asyncio
import importlib.util
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import anthropic
import yaml

# ── Config ────────────────────────────────────────────────────────────────────

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
        "min_votes": int(promote.get("min_votes", 3)),  # out of 4
    }

CFG = _load_config()

# ── Load llm_client from telegram-claude-bot ─────────────────────────────────

def _load_llm_client():
    llm_path = CFG["llm_client_path"] / "llm_client.py"
    if not llm_path.exists():
        print(f"ERROR: llm_client.py not found at {llm_path}", file=sys.stderr)
        sys.exit(1)
    # Ensure the parent dir is in sys.path (for dotenv .env loading)
    bot_dir = str(CFG["llm_client_path"])
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location("llm_client", llm_path)
    assert spec and spec.loader, f"Cannot load spec for {llm_path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod

_llm = _load_llm_client()

# ── Parse system prompt (current ruleset) ────────────────────────────────────

def load_current_ruleset() -> str:
    """Extract behavioral_rules and critical_rules text from build_system_prompt.py."""
    script = CFG["system_prompt_script"]
    if not script or not script.exists():
        return "(no current ruleset found)"
    src = script.read_text()
    # Extract content inside <behavioral_rules> and <critical_rules> tags
    rules = []
    for tag in ("critical_rules", "behavioral_rules"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", src, re.DOTALL)
        if m:
            rules.append(f"[{tag}]\n{m.group(1).strip()}")
    return "\n\n".join(rules) if rules else "(no rules found in script)"

# ── Lesson loading ────────────────────────────────────────────────────────────

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

# ── Committee prompts ─────────────────────────────────────────────────────────

_VOTER_SYSTEM = """You are evaluating whether a behavioral lesson should be promoted to a permanent system prompt rule for an AI assistant.

Current rules in the system prompt:
{ruleset}

Your task: vote on the candidate lesson below.

Respond in EXACTLY this format (no extra text):
VOTE: PROMOTE|REJECT|MERGE
REASON: <one sentence>

- PROMOTE: lesson is valuable, clear, and not already covered
- REJECT: lesson is too niche, already covered, or unclear
- MERGE: lesson overlaps with an existing rule and should be merged into it (state which rule)"""

_SONNET_SYSTEM = """You are the overlap checker on an AI committee. Your job is to identify if a lesson duplicates or significantly overlaps with existing system prompt rules.

Current rules in the system prompt:
{ruleset}

Respond in EXACTLY this format (no extra text):
VOTE: PROMOTE|REJECT|MERGE
REASON: <one sentence — if MERGE, name the rule it overlaps with>"""

_OPUS_SYSTEM = """You are the final arbiter. The committee has voted to promote a lesson to a permanent system prompt rule.

Current behavioral rules (numbered list):
{ruleset}

Write a single new rule line (max 15 words) that captures the lesson's essence. Match the style and tone of existing rules. Output ONLY the rule text, no explanation."""

_VOTER_USER = """Lesson to evaluate:
Title: {title}
Body: {body}"""

# ── Claude Sonnet/Opus calls ──────────────────────────────────────────────────

_anthropic = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def _call_claude(model: str, system: str, user: str, max_tokens: int = 200) -> str:
    try:
        resp = _anthropic.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        for block in resp.content:
            if block.type == "text":
                return block.text.strip()  # type: ignore[union-attr]
        return ""
    except Exception as e:
        return f"⚠️ Claude error: {e}"

# ── Vote parsing ──────────────────────────────────────────────────────────────

def _parse_vote(text: str) -> tuple[str, str]:
    """Return (vote, reason) from model response."""
    vote = "REJECT"
    reason = text[:100]
    for line in text.splitlines():
        if line.upper().startswith("VOTE:"):
            v = line.split(":", 1)[1].strip().upper()
            if v in ("PROMOTE", "REJECT", "MERGE"):
                vote = v
        elif line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    return vote, reason

# ── Per-lesson committee ──────────────────────────────────────────────────────

async def run_committee(lesson: dict, ruleset: str) -> dict:
    """Run all 4 voters in parallel, then optionally call Opus."""
    title = lesson["title"]
    body = lesson["body"]
    voter_sys = _VOTER_SYSTEM.format(ruleset=ruleset)
    sonnet_sys = _SONNET_SYSTEM.format(ruleset=ruleset)
    user_msg = _VOTER_USER.format(title=title, body=body)

    loop = asyncio.get_event_loop()

    # 3 free models + Sonnet in parallel
    tasks = {
        "minimax": loop.run_in_executor(None, _llm.chat_completion,
            [{"role": "user", "content": user_msg}], 200, 30, voter_sys),
        "kimi": loop.run_in_executor(None, _llm._call_single_model,
            "kimi", [{"role": "system", "content": voter_sys}, {"role": "user", "content": user_msg}], 200, 30),
        "gemini": loop.run_in_executor(None, _llm._call_single_model,
            "gemini", [{"role": "system", "content": voter_sys}, {"role": "user", "content": user_msg}], 200, 30),
        "sonnet": loop.run_in_executor(None, _call_claude,
            "claude-sonnet-4-6", sonnet_sys, user_msg, 200),
    }

    results_raw = await asyncio.gather(*tasks.values(), return_exceptions=True)
    keys = list(tasks.keys())

    votes = {}
    reasons = {}
    for key, raw in zip(keys, results_raw):
        if isinstance(raw, BaseException):
            text = f"⚠️ {raw}"
        elif isinstance(raw, tuple):
            text = raw[0] if raw[0] else f"⚠️ {raw[1]}"
        else:
            text = raw
        vote, reason = _parse_vote(text)
        votes[key] = vote
        reasons[key] = reason

    promote_count = sum(1 for v in votes.values() if v == "PROMOTE")
    merge_count = sum(1 for v in votes.values() if v == "MERGE")
    majority = promote_count + merge_count >= CFG["min_votes"]

    rule_text = None
    if majority:
        opus_sys = _OPUS_SYSTEM.format(ruleset=ruleset)
        rule_text = _call_claude("claude-opus-4-6", opus_sys, user_msg, 100)

    return {
        "title": title,
        "path": str(lesson["path"]),
        "votes": votes,
        "reasons": reasons,
        "promote_count": promote_count,
        "merge_count": merge_count,
        "decision": "PROMOTE" if majority else "REJECT",
        "rule_text": rule_text,
    }

# ── Apply promotion ───────────────────────────────────────────────────────────

def apply_promotion(lesson: dict, rule_text: str) -> None:
    """Update lesson frontmatter: status → promoted, add rule_text."""
    path = lesson["path"]
    raw = path.read_text()
    # Update status
    raw = re.sub(r"^status:.*$", "status: promoted", raw, flags=re.MULTILINE)
    # Add rule_text field if not present
    if "rule_text:" not in raw:
        raw = raw.replace("\nupdated:", f"\nrule_text: \"{rule_text}\"\nupdated:", 1)
    # write_md_file handles updated: stamp
    write_md_file(path, raw)

# ── Write rules to build_system_prompt.py ────────────────────────────────────

def write_rules_to_prompt(rule_texts: list[str]) -> list[int]:
    """Append new behavioral rules to build_system_prompt.py. Returns new rule numbers."""
    script = CFG["system_prompt_script"]
    if not script or not script.exists():
        raise FileNotFoundError(f"system_prompt_script not found: {script}")

    src = script.read_text()

    # Find last rule number in behavioral_rules block
    block_m = re.search(r"<behavioral_rules>(.*?)</behavioral_rules>", src, re.DOTALL)
    if not block_m:
        raise ValueError("No <behavioral_rules> block found in build_system_prompt.py")

    block = block_m.group(1)
    nums = [int(m) for m in re.findall(r"^(\d+)\.", block, re.MULTILINE)]
    next_num = (max(nums) + 1) if nums else 1

    new_lines = []
    assigned = []
    for i, rule in enumerate(rule_texts):
        n = next_num + i
        new_lines.append(f"{n}. {rule}")
        assigned.append(n)

    # Insert before </behavioral_rules>
    insert = "\n".join(new_lines) + "\n"
    src = src.replace("</behavioral_rules>", insert + "</behavioral_rules>")
    script.write_text(src)
    return assigned


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Promote pending lessons via AI committee")
    parser.add_argument("--apply", action="store_true", help="Apply promotions (update frontmatter)")
    parser.add_argument("--write-rules", action="store_true", help="Append promoted rules to build_system_prompt.py")
    parser.add_argument("--limit", type=int, default=0, help="Max lessons to process (0=all)")
    parser.add_argument("--json", action="store_true", dest="json_out", help="JSON output")
    args = parser.parse_args()

    ruleset = load_current_ruleset()
    candidates = load_pending_lessons()

    if not candidates:
        print("No pending lessons found.")
        return

    if args.limit:
        candidates = candidates[: args.limit]

    print(f"Processing {len(candidates)} pending lessons with 4-voter committee...\n")

    results = []
    for i, lesson in enumerate(candidates, 1):
        print(f"[{i}/{len(candidates)}] {lesson['title']}...", end=" ", flush=True)
        result = await run_committee(lesson, ruleset)
        results.append(result)
        decision = result["decision"]
        votes_str = " | ".join(f"{k}:{v[0]}" for k, v in result["votes"].items())
        print(f"{decision} ({votes_str})")

    # Summary
    promoted = [r for r in results if r["decision"] == "PROMOTE"]
    rejected = [r for r in results if r["decision"] == "REJECT"]

    if args.json_out:
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"\n{'='*60}")
    print(f"RESULTS: {len(promoted)} PROMOTE / {len(rejected)} REJECT (min_votes={CFG['min_votes']}/4)\n")

    for r in promoted:
        print(f"  PROMOTE: {r['title']}")
        print(f"    Rule:  {r['rule_text']}")
        for model, reason in r["reasons"].items():
            print(f"    {model}: [{r['votes'][model]}] {reason}")
        print()

    if rejected:
        print(f"  REJECTED ({len(rejected)}):")
        for r in rejected:
            votes_str = ", ".join(f"{k}:{v}" for k, v in r["votes"].items())
            print(f"    - {r['title']} ({votes_str})")

    if args.apply or args.write_rules:
        lesson_map = {str(l["path"]): l for l in candidates}

        if args.apply:
            print(f"\nApplying {len(promoted)} promotions (frontmatter)...")
            for r in promoted:
                lesson = lesson_map[r["path"]]
                apply_promotion(lesson, r["rule_text"])
                print(f"  Updated: {lesson['path'].name}")

        if args.write_rules:
            rule_texts = [r["rule_text"] for r in promoted if r["rule_text"]]
            if rule_texts:
                print(f"\nWriting {len(rule_texts)} rules to build_system_prompt.py...")
                try:
                    assigned = write_rules_to_prompt(rule_texts)
                    for r, n in zip(promoted, assigned):
                        print(f"  Rule {n}: {r['rule_text']}")
                    print("Rebuild: run `python3 ~/.claude/scripts/build_system_prompt.py` to verify.")
                except Exception as e:
                    print(f"  ERROR writing rules: {e}")
            else:
                print("\nNo rule texts to write (all Opus calls failed?).")

        print("Done.")
    else:
        if promoted:
            print(f"\n(dry run — use --apply to update frontmatter, --write-rules to patch build_system_prompt.py)")

if __name__ == "__main__":
    asyncio.run(main())
