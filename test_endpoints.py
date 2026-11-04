#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 TVBox 多仓 / 单仓接口可用性与速度。

复用 build.py 的宽容解析（去注释 / raw_decode / 反爬页检测），
对每个候选地址做 3 次采样取中位数延迟，最后按「可用 + 快 + 内容丰富」排序。

用法：
    python test_endpoints.py            # 全量测试
    python test_endpoints.py --write    # 测试并把结果写回 dist/多仓订阅.json
"""
import argparse
import json
import os
import statistics
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
CACHE = os.path.join(HERE, ".cache")

# 直接借用 build.py 里已经调好的解析器，避免两套逻辑不一致
sys.path.insert(0, HERE)
from build import lenient_json, looks_valid, idn_encode, UA, TIMEOUT  # noqa: E402

SAMPLES = 3


class Endpoint:
    def __init__(self, source, name, urls, kind_hint="-"):
        self.source = source
        self.name = name
        self.urls = urls
        self.kind_hint = kind_hint
        self.used = None
        self.ok = False
        self.latency = None      # 3 次采样中位数(ms)
        self.samples = []
        self.size = 0
        self.kind = "-"
        self.sites = 0
        self.parses = 0
        self.lives = 0
        self.urls_n = 0
        self.err = ""
        self.score = 0.0


def probe(url):
    """单次探测：返回 (ok, latency_ms, size, payload, err)。"""
    target = idn_encode(url)
    t0 = time.time()
    try:
        r = requests.get(target, timeout=TIMEOUT, allow_redirects=True,
                         headers={"User-Agent": UA,
                                  "Accept": "application/json,text/plain,*/*"})
        dt = (time.time() - t0) * 1000
        if r.status_code != 200:
            return False, dt, len(r.content), None, f"HTTP {r.status_code}"
        ctype = (r.headers.get("content-type") or "").lower()
        raw = r.content
        # 反爬：返回图片
        if ctype.startswith("image/") or raw[:3] in (b"\xff\xd8\xff", b"GIF", b"BM"):
            return False, dt, len(raw), None, f"返回图片（反爬挑战, {ctype}）"
        # 代理首页 / HTML 错误页陷阱：ghproxy 这类镜像失效时会 200 返回自己的网页
        head = raw[:200].lstrip().lower()
        if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
            return False, dt, len(raw), None, "返回 HTML 网页（多半是代理/错误页）"
        data = lenient_json(r.text)
        if not looks_valid(data):
            return False, dt, len(raw), None, "结构不是 TVBox 配置"
        return True, dt, len(raw), data, ""
    except Exception as e:  # noqa: BLE001
        dt = (time.time() - t0) * 1000
        return False, dt, 0, None, f"{type(e).__name__}: {str(e)[:80]}"


def classify(data):
    """统计配置内容。"""
    if not isinstance(data, dict):
        return "-", 0, 0, 0, 0
    if isinstance(data.get("urls"), list) and data["urls"]:
        return "多仓", 0, 0, 0, len(data["urls"])
    if "sites" in data:
        return ("单仓", len(data.get("sites") or []),
                len(data.get("parses") or []),
                len(data.get("lives") or []), 0)
    return "-", 0, 0, 0, 0


def test_one(ep):
    errors = []
    for u in ep.urls:
        ep.samples = []
        payload = None
        for i in range(SAMPLES):
            ok, dt, size, data, err = probe(u)
            ep.samples.append(round(dt))
            if not ok:
                errors.append(f"{u} → {err}")
                payload = None
                break
            payload = data
            ep.size = size
            if i < SAMPLES - 1:
                time.sleep(0.3)          # 拉开间隔，避免缓存命中虚高
        if payload is None:
            continue                     # 换下一个备用地址
        ep.ok = True
        ep.used = u
        ep.latency = int(statistics.median(ep.samples))
        ep.err = ""
        ep.kind, ep.sites, ep.parses, ep.lives, ep.urls_n = classify(payload)
        # 评分：越快越好，内容越多越好；>3s 基本等于不可用
        if ep.kind == "多仓":
            content_k = ep.urls_n * 1.5
        else:
            content_k = ep.sites * 0.5 + ep.parses * 0.5 + ep.lives
        ep.score = content_k / max(ep.latency, 120) * 1000
        if ep.latency > 3000:
            ep.score *= 0.4          # 慢到影响体验，降权但不直接淘汰
        return ep
    ep.err = " | ".join(errors) if errors else "无候选地址"
    return ep


def load_candidates(extra_file=None):
    """候选 = sources.json 全部源 + 页面新增源（含备用地址）。"""
    eps = []
    cfg = json.load(open(os.path.join(HERE, "sources.json"), encoding="utf-8"))
    for s in cfg["sources"]:
        eps.append(Endpoint("原仓库", s["name"], list(s.get("urls") or [])))
    for s in extra_file or []:
        eps.append(Endpoint(s.get("source", "新页面"), s["name"], list(s["urls"])))
    return eps


# 国内直连 raw.githubusercontent.com 基本不通，写进订阅时统一换成加速前缀
GH_MIRRORS = ["https://gh-proxy.com/", "https://ghfast.top/"]


def prefer_domestic(url):
    """raw.githubusercontent.com 且用户在国内访问时，换成可用的加速地址。"""
    if "raw.githubusercontent.com" not in url or url.startswith("https://gh"):
        return url
    for m in GH_MIRRORS:
        ok, _, size, _, _ = probe(m + url)
        if ok and size > 1000:
            return m + url
    return url


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="把结果写入 dist/多仓订阅.json")
    ap.add_argument("--extra", default=os.path.join(HERE, "extra_sources.json"))
    args = ap.parse_args()

    extra = None
    if os.path.exists(args.extra):
        extra = json.load(open(args.extra, encoding="utf-8"))
        print(f"额外候选 {len(extra)} 条（来自 {os.path.basename(args.extra)}）")

    eps = load_candidates(extra)
    print(f"共 {len(eps)} 个接口，每个 {SAMPLES} 次采样\n")
    print(f"{'状态':<5}{'接口':<16}{'类型':<6}{'延迟':>7}{'体积':>9}  内容 / 错误")
    print("-" * 96)
    for ep in eps:
        test_one(ep)
        if ep.ok:
            print(f"{'OK':<5}{ep.name:<16}{ep.kind:<6}{ep.latency:>5}ms{ep.size:>8}B  "
                  f"站点{ep.sites} 解析{ep.parses} 直播{ep.lives} 源{ep.urls_n}")
        else:
            age = f"{ep.latency}ms" if ep.latency else "-"
            print(f"{'FAIL':<5}{ep.name:<16}{'-':<6}{age:>7}{ep.size:>8}B  {ep.err}")

    ok = [e for e in eps if e.ok]
    bad = [e for e in eps if not e.ok]

    # 写回多仓订阅：所有存活接口，按评分降序（最快最丰富的排最前）
    ok_sorted = sorted(ok, key=lambda e: -e.score)
    print(f"\n可用 {len(ok)} 个，失效 {len(bad)} 个")
    print("\n按评分排序（多仓顺序将照此排列）：")
    for i, e in enumerate(ok_sorted, 1):
        print(f"  {i:>2}. {e.name:<16} {e.kind}  {e.latency:>4}ms  评分 {e.score:6.2f}")

    result = {
        "tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "endpoints": [
            {k: getattr(e, k) for k in
             ("source", "name", "used", "ok", "latency", "samples", "size", "kind",
              "sites", "parses", "lives", "urls_n", "score", "err", "urls")}
            for e in eps
        ],
    }
    os.makedirs(DIST, exist_ok=True)
    json.dump(result, open(os.path.join(DIST, "接口测试.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    if args.write:
        subs = []
        for e in ok_sorted:
            u = prefer_domestic(e.used)
            if u != e.used:
                print(f"  · {e.name}: raw → 加速 {u.split('/')[2]}")
            subs.append({"name": e.name, "url": u})
        mp = os.path.join(DIST, "多仓订阅.json")
        json.dump({"urls": subs}, open(mp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n✅ 已写入 {mp}（{len(subs)} 个源）")
    else:
        print("\n（未写回，加 --write 生效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
