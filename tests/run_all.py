#!/usr/bin/env python3
"""依次跑三套脚本式测试并汇总 passed/failed/skipped。"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [
    ('RAG/压缩', 'tests/test_rag_compress_functional.py'),
    ('定时提醒', 'tests/test_reminder_functional.py'),
    ('P0/P1/安全', 'tests/test_p0_p1_security.py'),
    ('身份拦截', 'tests/test_identity_guard.py'),
]

FOOTER_RE = re.compile(
    r'(\d+)\s+passed,\s*(\d+)\s+failed(?:,\s*(\d+)\s+skipped)?',
    re.I,
)


def run_one(label: str, rel: str) -> tuple[int, int, int, int]:
    path = ROOT / rel
    print(f'\n######## {label}: {rel} ########')
    proc = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or '') + (('\n' + proc.stderr) if proc.stderr else '')
    sys.stdout.write(proc.stdout or '')
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    matches = list(FOOTER_RE.finditer(proc.stdout or ''))
    if matches:
        m = matches[-1]
        passed = int(m.group(1))
        failed = int(m.group(2))
        skipped = int(m.group(3) or 0)
    else:
        passed, skipped = 0, 0
        failed = 1 if proc.returncode else 0
        print(f'  (未解析到计数器，returncode={proc.returncode})')
    print(f'-- {label}: {passed} passed, {failed} failed, {skipped} skipped '
          f'(exit {proc.returncode})')
    return passed, failed, skipped, proc.returncode


def main() -> int:
    tot_p = tot_f = tot_s = 0
    any_fail = False
    for label, rel in SCRIPTS:
        p, f, s, rc = run_one(label, rel)
        tot_p += p
        tot_f += f
        tot_s += s
        if rc != 0 or f:
            any_fail = True
    print(f'\n======== 总计: {tot_p} passed, {tot_f} failed, {tot_s} skipped ========')
    return 1 if any_fail else 0


if __name__ == '__main__':
    raise SystemExit(main())
