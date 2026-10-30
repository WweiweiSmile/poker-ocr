#!/usr/bin/env python3
"""把 `data/hand_outbox/pending/` 里的已结束手牌同步到手牌库。

    # 预览
    .venv/bin/python tools/sync_hand_outbox.py --dry-run

    # 同步到 replay-console
    .venv/bin/python tools/sync_hand_outbox.py \
      --url http://127.0.0.1:8010 --token "$REPLAY_TOKEN"

    # 重试 failed/
    .venv/bin/python tools/sync_hand_outbox.py --retry-failed

优先级：

1. `POST /api/hands/ingest`（结构化、推荐；服务端直接吃 HandRecord v2）；
2. 404/405 时回退到 `POST /api/hands/import`（旧服务端兼容）。

成功才把文件移到 `sent/`；失败留在 `pending/`，超过 `--max-failures`
（默认 5，按 `.error` 文件计数）移到 `failed/`，避免堵住队列。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.hand_outbox import DEFAULT_OUTBOX_DIR, HandOutbox  # noqa: E402


def _post(url: str, payload: dict, token: str | None, timeout: float) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"raw": raw}
    return data if isinstance(data, dict) else {"data": data}


def _ingest(base: str, record: dict, token: str | None,
            timeout: float) -> dict:
    return _post(base.rstrip("/") + "/api/hands/ingest",
                 {"source": "live", "records": [record]}, token, timeout)


def _import_fallback(base: str, record: dict, token: str | None,
                     timeout: float) -> dict:
    return _post(base.rstrip("/") + "/api/hands/import", {
        "format": "live",
        "content": json.dumps([record], ensure_ascii=False),
        "dryRun": False,
        # 复盘允许 approx；A/B 是否只跑 exact 由实验侧决定。
        "onlyExact": False,
        "allowInvalid": False,
    }, token, timeout)


def _http_status(exc: Exception) -> int | None:
    if isinstance(exc, urllib.error.HTTPError):
        return int(exc.code)
    return None


def _fail(path: Path, error: str, max_failures: int, outbox: HandOutbox) -> str:
    """失败先留在 pending 并记次数；达到上限才进 failed/。"""
    sidecar = path.with_suffix(".error")
    try:
        attempts = int(sidecar.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        attempts = 0
    attempts += 1
    if attempts >= max(1, int(max_failures)):
        outbox.mark_failed(path, f"第 {attempts} 次失败：{error}")
        return f"（已进 failed/，第 {attempts} 次）"
    sidecar.write_text(f"{attempts}\n{error}"[:4000], encoding="utf-8")
    return f"（第 {attempts}/{max_failures} 次，留在 pending）"


def main() -> int:
    ap = argparse.ArgumentParser(description="同步手牌 outbox 到手牌库")
    ap.add_argument("--outbox", type=Path, default=DEFAULT_OUTBOX_DIR,
                    help=f"outbox 目录（默认 {DEFAULT_OUTBOX_DIR}）")
    ap.add_argument("--url", default="http://127.0.0.1:8010",
                    help="replay-console 地址")
    ap.add_argument("--token", default=None,
                    help="REPLAY_TOKEN；也可以用环境变量")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--limit", type=int, default=200,
                    help="本次最多同步几手（默认 200）")
    ap.add_argument("--max-failures", type=int, default=5,
                    help="同一手连续失败多少次后移到 failed/")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印，不发请求、不移动文件")
    ap.add_argument("--retry-failed", action="store_true",
                    help="先把 failed/ 移回 pending/")
    ap.add_argument("--no-fallback", action="store_true",
                    help="不兼容旧 /api/hands/import")
    args = ap.parse_args()

    token = args.token or ""
    if not token:
        import os
        token = (os.environ.get("REPLAY_TOKEN") or "").strip()
    outbox = HandOutbox(args.outbox)
    if args.retry_failed:
        moved = outbox.retry_failed()
        print(f"failed → pending：{moved} 手")

    paths = outbox.pending()[:max(0, args.limit)]
    if not paths:
        print(f"没有待同步手牌：{outbox.pending_dir}")
        return 0

    sent = failed = 0
    for index, path in enumerate(paths, 1):
        try:
            record = outbox.load(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[{index}/{len(paths)}] 坏文件 {path.name}：{exc}")
            detail = _fail(path, f"坏文件：{exc}", args.max_failures, outbox)
            print(f"  → {detail}")
            failed += 1
            continue
        uid = str(record.get("uid") or record.get("hand_id") or path.stem)
        if args.dry_run:
            print(f"[{index}/{len(paths)}] 将发送 {uid}（{len(record)} 字段）")
            sent += 1
            continue
        try:
            result = _ingest(args.url, record, token, args.timeout)
        except Exception as exc:  # noqa: BLE001
            status = _http_status(exc)
            if status in (404, 405) and not args.no_fallback:
                try:
                    result = _import_fallback(args.url, record, token,
                                              args.timeout)
                except Exception as fallback_exc:  # noqa: BLE001
                    print(f"[{index}/{len(paths)}] 失败 {uid}："
                          f"{type(fallback_exc).__name__}: {fallback_exc}")
                    detail = _fail(path, str(fallback_exc),
                                   args.max_failures, outbox)
                    print(f"  → {detail}")
                    failed += 1
                    continue
            else:
                print(f"[{index}/{len(paths)}] 失败 {uid}："
                      f"{type(exc).__name__}: {exc}")
                detail = _fail(path, str(exc), args.max_failures, outbox)
                print(f"  → {detail}")
                failed += 1
                continue
        outbox.mark_sent(path)
        detail = ""
        if isinstance(result, dict):
            if result.get("duplicates"):
                detail = f"（重复 {result.get('duplicates')}）"
            elif result.get("inserted") is not None:
                detail = f"（新增 {result.get('inserted')}）"
        print(f"[{index}/{len(paths)}] 已发送 {uid}{detail}")
        sent += 1

    stats = outbox.stats()
    print(f"完成：发送 {sent}，失败 {failed}；"
          f"pending {stats.pending}，sent {stats.sent}，failed {stats.failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
