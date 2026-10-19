"""Cross-check per-run token counts from `claude -p --output-format json`
against agent-harness's own telemetry CLI (collect + aggregate --by project).

Telemetry runs with USERPROFILE pointed at ahbench/telhome, whose
.claude/projects is a junction to the real ~/.claude/projects, so collection
reads the real session logs but writes its event store under telhome rather
than into the operator's real ~/.agent-harness/telemetry.

It also re-counts each session log directly, once per assistant line and once
per unique message id, to explain any gap between the two sources.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run import BENCH, RESULTS, PIPX, session_files  # noqa: E402

TELHOME = BENCH / "telhome"
TELEMETRY = PIPX / "Scripts" / "agent-harness-telemetry.exe"
SINCE = "2026-10-12T13:00:00Z"


def norm(p: str) -> str:
    return p.replace("/", "\\").rstrip("\\").lower()


def telemetry() -> dict:
    env = dict(os.environ, USERPROFILE=str(TELHOME), HOME=str(TELHOME))
    c = subprocess.run([str(TELEMETRY), "collect", "--agent", "claude-code"], env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("collect:", (c.stdout + c.stderr).strip()[-300:])
    a = subprocess.run([str(TELEMETRY), "aggregate", "--by", "project", "--since", SINCE], env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = {}
    for line in a.stdout.splitlines():
        m = re.match(r"project=(.*): events=(\d+) input_tokens=(\d+) output_tokens=(\d+)", line)
        if m:
            out[norm(m.group(1))] = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return out


def recount(session_id: str) -> dict:
    per_line = [0, 0]
    unique = {}
    for f in session_files(session_id):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            msg = o.get("message")
            if o.get("type") != "assistant" or not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            u = msg["usage"]
            inp = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                     "cache_read_input_tokens"))
            per_line[0] += inp
            per_line[1] += int(u.get("output_tokens") or 0)
            unique[msg.get("id") or id(o)] = (inp, int(u.get("output_tokens") or 0))
    return {"per_line_in": per_line[0], "per_line_out": per_line[1],
            "unique_in": sum(v[0] for v in unique.values()),
            "unique_out": sum(v[1] for v in unique.values())}


def main() -> None:
    tel = telemetry()
    rows = [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip()]
    report = []
    print(f"\n{'run':<40} {'json in':>10} {'tel in':>10} {'in ratio':>8} {'json out':>8} {'tel out':>8} "
          f"{'uniq in':>10} {'uniq out':>8}")
    for r in rows:
        t = tel.get(norm(r["cwd"]))
        rc = recount(r["session_id"]) if r.get("session_id") else {}
        # result JSON totals include every model (e.g. the small Haiku side calls);
        # session logs only carry the main conversation's model.
        main_model = r.get("model_usage", {}).get(r["model_requested"], {})
        main_in = sum(int(main_model.get(k) or 0) for k in ("inputTokens", "cacheReadInputTokens",
                                                             "cacheCreationInputTokens"))
        main_out = int(main_model.get("outputTokens") or 0)
        rec = {"run_id": r["run_id"], "arm": r["arm"], "json_in_main": main_in, "json_out_main": main_out,
               "json_in_all": r["total_input_tokens"], "json_out_all": r["output_tokens"],
               "tel_events": t and t[0], "tel_in": t and t[1], "tel_out": t and t[2], **rc}
        report.append(rec)
        if t:
            print(f"{r['run_id'][:40]:<40} {main_in:>10,} {t[1]:>10,} {t[1] / main_in if main_in else 0:>8.3f} "
                  f"{main_out:>8,} {t[2]:>8,} {rc.get('unique_in', 0):>10,} {rc.get('unique_out', 0):>8,}")
        else:
            print(f"{r['run_id'][:40]:<40} not found in telemetry aggregate")
    (BENCH / "crosscheck.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    found = [x for x in report if x["tel_in"]]
    if found:
        tin = sum(x["tel_in"] for x in found); jin = sum(x["json_in_main"] for x in found)
        tout = sum(x["tel_out"] for x in found); jout = sum(x["json_out_main"] for x in found)
        uin = sum(x.get("unique_in", 0) for x in found); uout = sum(x.get("unique_out", 0) for x in found)
        print(f"\nmatched {len(found)}/{len(report)} runs")
        print(f"input  : telemetry {tin:,} vs result-json {jin:,} (ratio {tin / jin:.3f}); unique-message recount {uin:,} ({uin / jin:.3f})")
        print(f"output : telemetry {tout:,} vs result-json {jout:,} (ratio {tout / jout:.3f}); unique-message recount {uout:,} ({uout / jout:.3f})")


if __name__ == "__main__":
    main()
