#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox / 影视仓 个人配置构建器
=============================
参考 Lightconer/tvbox-ysc-config 的聚合思路，但修正了一个关键问题：

    不同源里的 csp_XXX 站点依赖"各自源的 spider（jar/js）"，
    把多个源的站点塞进同一个 spider 里，跨源站点会大面积失效。

因此本脚本采用「分类合并」策略：
  · 站点(sites)      -> 只从"基准源"取，保证与基准源的 spider 完全匹配
  · 可移植站点       -> 其它源里 type=0/1 的纯 HTTP JSON/XML 站点可以安全并入
  · 解析(parses)     -> 跨源合并去重（解析器与 spider 无关，天然可移植）
  · 直播(lives)      -> 跨源合并去重（m3u/txt 地址，天然可移植）
  · 配置(flags/ads/doh/rules/ijk/hosts) -> 从基准源继承并合并增强

产物：
  dist/单仓精选.json    一个地址搞定，日常首选
  dist/多仓订阅.json    App 内可切换各源，作为备份/换源
  dist/配置报告.md      本次构建明细（源状态、站点增减、被过滤项）
  dist/预览.html        可视化预览

用法：
  python build.py                 # 完整构建
  python build.py --base xiaoma   # 指定基准源
  python build.py --offline       # 不联网，只用本地缓存
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.parse

try:
    import requests
except ImportError:
    sys.exit("缺少依赖：pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
CACHE = os.path.join(HERE, ".cache")
SRC_FILE = os.path.join(HERE, "sources.json")

UA = "Mozilla/5.0"          # 简洁 UA：部分接口对完整浏览器 UA 反而下发挑战页
TIMEOUT = 15
RETRIES = 2

# 引流 / 广告 / 售卖类站点，个人自用一律剔除
BLOCK_PAT = re.compile(
    r"QQ频道|Q群|QQ群|扣群|加群|进群|群聊|公众号|电报|飞机|t\.me/|@[A-Za-z0-9_]{5,}"
    r"|贩卖|售卖|出售|引流|推广|赞助|打赏|捐助|捐赠|定制|接单|客服|商务|合作"
    r"|免费分享|本接口免费|谨防|上当|骗",
    re.IGNORECASE,
)
# 但含这些词的站点是正常功能，不应被误杀
ALLOW_PAT = re.compile(r"设置|Token|token|配置|中心|切换", re.IGNORECASE)

TVBOX_KEYS = ("sites", "spiders", "lives", "store", "exts", "parses", "flags", "urls")


# ---------------------------------------------------------------- 抓取
def idn_encode(url: str) -> str:
    """中文域名 -> punycode，避免 DNS 解析失败。"""
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    try:
        host = host.encode("idna").decode("ascii")
    except Exception:
        pass
    netloc = f"{host}:{p.port}" if p.port else host
    return urllib.parse.urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))


def lenient_json(text: str):
    """宽容解析：去 BOM、剔除整行 // 注释、兼容未转义控制字符、兼容多段拼接。"""
    text = text.lstrip("\ufeff").strip()
    text = "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("//")).strip()
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        obj, _ = json.JSONDecoder(strict=False).raw_decode(text)
        return obj


def looks_valid(d) -> bool:
    if not isinstance(d, dict):
        return False
    if any(k in d for k in TVBOX_KEYS):
        return True
    return isinstance(d.get("urls"), list) and bool(d["urls"])


def fetch(url: str):
    """抓取单个地址。反爬返回图片/HTML 时判为失败。"""
    target = idn_encode(url)
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(target, headers={"User-Agent": UA,
                                              "Accept": "application/json,text/plain,*/*"},
                             timeout=TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").lower()
            raw = r.content
            if ctype.startswith("image/") or raw[:3] in (b"\xff\xd8\xff", b"GIF", b"BM"):
                raise ValueError(f"返回图片而非配置（反爬挑战，{ctype or 'binary'}）")
            data = lenient_json(r.text)
            if not looks_valid(data):
                raise ValueError("结构不是 TVBox 配置")
            return data, url
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < RETRIES:
                continue
    raise last


def cache_path(sid: str) -> str:
    return os.path.join(CACHE, f"{sid}.json")


def load_source(src: dict, offline: bool):
    """按主/备顺序抓取；全失败则回退本地缓存。返回 (data, used_url, state)。"""
    if not offline:
        for url in src.get("urls", []):
            try:
                data, used = fetch(url)
                os.makedirs(CACHE, exist_ok=True)
                with open(cache_path(src["id"]), "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                return data, used, "online"
            except Exception as e:  # noqa: BLE001
                print(f"   ! {src['name']:<10} {url} -> {str(e)[:70]}")
    p = cache_path(src["id"])
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f), "(本地缓存)", "cache"
    return None, None, "dead"


# ---------------------------------------------------------------- 清洗
def site_name(s) -> str:
    return str(s.get("name") or "").replace("\u200b", "").strip()


def is_junk(s) -> bool:
    n = site_name(s)
    if not n:
        return True
    if ALLOW_PAT.search(n):
        return False
    return bool(BLOCK_PAT.search(n))


def clean_sites(sites, drop_junk=True, seen=None):
    """去空、去引流、按 key/name 去重。返回 (保留, 被剔除列表)"""
    seen = seen if seen is not None else set()
    kept, dropped = [], []
    for s in sites or []:
        if not isinstance(s, dict):
            continue
        api = str(s.get("api") or "")
        if not api:
            dropped.append(("缺少 api", site_name(s)))
            continue
        if drop_junk and is_junk(s):
            dropped.append(("引流/广告", site_name(s)))
            continue
        sig = (site_name(s), api)
        if sig in seen:
            dropped.append(("重复", site_name(s)))
            continue
        seen.add(sig)
        kept.append(s)
    return kept, dropped


def ensure_unique_keys(sites):
    """保证 sites[].key 唯一，冲突时加后缀（TVBox 用 key 定位站点）。"""
    used = set()
    out = []
    for s in sites:
        s = dict(s)
        k = str(s.get("key") or "").strip() or site_name(s)
        base, i = k, 2
        while k in used:
            k = f"{base}_{i}"
            i += 1
        used.add(k)
        s["key"] = k
        out.append(s)
    return out


def abs_asset(u, base_url):
    """把 ./libs/js/xxx.js 这类相对引用还原为绝对地址（否则换托管后 404）。"""
    if not isinstance(u, str) or not u.strip():
        return u
    u = u.strip()
    if u.startswith(("http://", "https://", "csp_", "py_", "js:", "file://")):
        return u
    if not base_url:
        return u
    return urllib.parse.urljoin(base_url, u)


_ASSET_CACHE = {}
_ASSET_LOADED = False
_RESOLVE = {}          # 相对引用 -> 绝对地址（None 表示失效）


def _load_asset_cache():
    global _ASSET_LOADED
    if _ASSET_LOADED:
        return
    _ASSET_LOADED = True
    p = os.path.join(CACHE, "assets.json")
    if os.path.exists(p):
        try:
            _ASSET_CACHE.update(json.load(open(p, encoding="utf-8")))
        except Exception:
            pass


def save_asset_cache():
    os.makedirs(CACHE, exist_ok=True)
    json.dump(_ASSET_CACHE, open(os.path.join(CACHE, "assets.json"), "w", encoding="utf-8"),
              ensure_ascii=False)


def asset_alive(url: str, offline: bool, force: bool = False) -> bool:
    """实测资源是否可取（js / jar / m3u / txt）。带磁盘缓存，避免重复请求。
    force=True 时忽略缓存重新探测（用于复核并发阶段的疑似失败）。"""
    _load_asset_cache()
    if not force and url in _ASSET_CACHE:
        return _ASSET_CACHE[url]
    if offline:
        return True
    ok = False
    for _ in range(2):
        try:
            r = requests.get(idn_encode(url), headers={"User-Agent": UA}, timeout=10,
                             allow_redirects=True)
            ctype = (r.headers.get("content-type") or "").lower()
            ok = r.status_code == 200 and len(r.content) > 200 and not ctype.startswith("text/html")
            if ok:
                break
        except Exception:
            ok = False
    _ASSET_CACHE[url] = ok
    return ok


# 已知的失效目录映射：老刘备源里 ./lib/ 实际应为 ./libs/js/
REMAP = (("/lib/", "/libs/js/"), ("/libs/js/js/", "/libs/js/"))


def prefetch_assets(refs, base_url, offline, workers=6):
    """并发预取所有相对引用，填充 _RESOLVE，避免逐站点串行等待。"""
    _load_asset_cache()
    todo = sorted({r for r in refs if isinstance(r, str) and r.startswith(("./", "../"))
                   and r not in _RESOLVE})
    if not todo:
        return
    if offline or not base_url:
        for r in todo:
            _RESOLVE[r] = abs_asset(r, base_url) if base_url else None
        return

    def work(r):
        cand = abs_asset(r, base_url)
        if not cand or cand == r:
            return r, None
        if asset_alive(cand, offline):
            return r, cand
        for a, b in REMAP:
            if a in cand:
                alt = cand.replace(a, b)
                if asset_alive(alt, offline):
                    return r, alt
        return r, None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r, final in ex.map(work, todo):
            _RESOLVE[r] = final

    # 并发请求易被 CDN 限流，失败的引用必须串行复核，避免误杀站点
    failed = [r for r in todo if not _RESOLVE.get(r)]
    if failed:
        for r in failed:
            cand = abs_asset(r, base_url)
            if cand and cand != r and asset_alive(cand, offline, force=True):
                _RESOLVE[r] = cand
                continue
            for a, b in REMAP:
                if cand and a in cand:
                    alt = cand.replace(a, b)
                    if asset_alive(alt, offline, force=True):
                        _RESOLVE[r] = alt
                        break
            time.sleep(0.25)

    ok = sum(1 for v in _RESOLVE.values() if v)
    print(f"   资源校验：{len(todo)} 个引用，可用 {ok} 个"
          + (f"（复核后恢复 {sum(1 for r in failed if _RESOLVE.get(r))} 个）" if failed else ""))


def collect_refs(obj, out):
    """收集对象内所有 ./ 或 ../ 开头的字符串。"""
    if isinstance(obj, str):
        if obj.startswith(("./", "../")):
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            collect_refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_refs(v, out)


def resolve_asset(u: str, base_url: str, offline: bool):
    """相对路径 -> 绝对路径 + 可达性校验（优先用预取结果）。返回 (最终地址, 是否可用)。"""
    if not isinstance(u, str) or not u.strip():
        return u, False
    u = u.strip()
    if u.startswith(("http://", "https://")):
        return u, True
    if u.startswith(("csp_", "py_")):
        return u, True
    if u in _RESOLVE:
        final = _RESOLVE[u]
        return (final, True) if final else (u, False)
    cand = abs_asset(u, base_url)
    if cand == u:            # 无法解析的相对引用（如 "yqk"）
        return u, False
    if asset_alive(cand, offline):
        _RESOLVE[u] = cand
        return cand, True
    for a, b in REMAP:       # 失效路径尝试重映射
        if a in cand:
            alt = cand.replace(a, b)
            if asset_alive(alt, offline):
                _RESOLVE[u] = alt
                return alt, True
    _RESOLVE[u] = None
    return cand, False


# 站点里一旦缺失就必然打不开的依赖字段
CRITICAL_FIELDS = {"api", "jar", "spider", "url", "data", "js", "json", "txt", "local", "site"}
EXT_CRITICAL = CRITICAL_FIELDS


def fix_paths(obj, base_url, offline, fails, key=""):
    """递归绝对化 ext 等字段里的相对路径。返回 (新对象, 是否全部成功)。
    fails 收集 (关键字段名, 失败的相对路径)。"""
    if isinstance(obj, str):
        s = obj.strip()
        # ext 常以 JSON 字符串形式存放配置，先解析再修，避免漏改
        if key == "ext" and s.startswith("{") and s.endswith("}"):
            try:
                inner = json.loads(s)
                nv, ok = fix_paths(inner, base_url, offline, fails, "ext")
                return (json.dumps(nv, ensure_ascii=False) if ok else obj), ok
            except Exception:
                return obj, True
        if s.startswith(("./", "../")):
            final, ok = resolve_asset(s, base_url, offline)
            if not ok:
                fails.append((key, s))
                return obj, False
            return final, True
        return obj, True
    if isinstance(obj, list):
        okall, out = True, []
        for v in obj:
            nv, ok = fix_paths(v, base_url, offline, fails, key)
            out.append(nv)
            okall = okall and ok
        return out, okall
    if isinstance(obj, dict):
        okall, out = True, {}
        for k, v in obj.items():
            nv, ok = fix_paths(v, base_url, offline, fails, k)
            out[k] = nv
            okall = okall and ok
        return out, okall
    return obj, True


def portable(s) -> bool:
    """type 0/1 的纯 http 接口站点不依赖 spider，可跨源合并。"""
    api = str(s.get("api") or "")
    return api.startswith("http") and s.get("type") in (0, 1)


def absolutize_spider(spider, base_url):
    """把 ./fty.jar 这种相对 spider 还原成绝对地址，否则换仓库托管后会 404。"""
    if not spider or not isinstance(spider, str):
        return spider
    url, _, md5 = spider.partition(";")
    if url.startswith(("http://", "https://")):
        return spider
    if base_url:
        absurl = urllib.parse.urljoin(base_url, url)
        return f"{absurl};{md5}" if md5 else absurl
    return spider


def merge_list(items, key_fields, limit=None):
    """列表去重合并，按 key_fields 依次作为唯一标识。"""
    seen, out = set(), []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        ident = None
        for f in key_fields:
            if it.get(f):
                ident = str(it[f]).strip()
                break
        if not ident:
            ident = json.dumps(it, ensure_ascii=False, sort_keys=True)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(it)
        if limit and len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 构建
def build(args):
    cfg = json.load(open(SRC_FILE, encoding="utf-8"))
    sources = cfg["sources"]
    os.makedirs(DIST, exist_ok=True)

    print("① 抓取配置源")
    alive = []
    for src in sources:
        print(f"   - {src['name']}")
        data, used, state = load_source(src, args.offline)
        if not data:                      # 瞬时抖动：冷却后整源再试一次
            time.sleep(1.5)
            data, used, state = load_source(src, args.offline)
        kind = None
        n = 0
        if data:
            kind = "单仓" if "sites" in data else ("多仓" if "urls" in data else None)
            n = len(data.get("sites") or data.get("urls") or [])
        if data and kind:
            alive.append(dict(id=src["id"], name=src["name"], data=data,
                              used=used, state=state, kind=kind, n=n,
                              primary=(src.get("urls") or [None])[0]))
            print(f"     ✓ {kind} {n} 项  ({state})")
        else:
            print("     ✗ 全部地址失败，且无缓存")

    singles = [a for a in alive if a["kind"] == "单仓"]
    multis = [a for a in alive if a["kind"] == "多仓"]
    if not singles:
        sys.exit("没有可用的单仓源，无法构建。")

    # ② 选基准源
    print("\n② 选择基准源")
    prefer = cfg.get("prefer_base") or []
    chosen = None
    if args.base:
        chosen = next((a for a in singles if a["id"] == args.base), None)
    if not chosen:
        for pid in prefer:
            chosen = next((a for a in singles if a["id"] == pid and a["state"] == "online"), None)
            if chosen:
                break
    if not chosen:
        chosen = max(singles, key=lambda a: a["n"])
    print(f"   → {chosen['name']}（{chosen['n']} 个站点，spider 匹配该源）")

    base = chosen["data"]
    merged = {}          # 站点/列表之外的顶层字段
    STATS = {"dropped": [], "added_portable": [], "base": chosen["name"]}

    for k, v in base.items():
        if k in ("sites", "lives", "parses", "ads", "doh", "rules", "flags", "exts",
                 "hosts", "ijk", "danmaku", "wallpaper", "logo", "spider", "warningText"):
            continue
        merged[k] = v

    # ③ 站点：基准源全量 + 其它源可移植站点
    print("\n③ 清洗站点")
    seen = set()
    base_sites, dropped = clean_sites(base.get("sites"), drop_junk=not args.keep_all, seen=seen)
    STATS["dropped"] += dropped
    print(f"   基准源 {len(base.get('sites') or [])} → 保留 {len(base_sites)}（剔除 {len(dropped)}）")

    extra = []
    if not args.no_portable:
        for a in singles:
            if a["id"] == chosen["id"]:
                continue
            add, drop = clean_sites([s for s in (a["data"].get("sites") or []) if portable(s)],
                                    drop_junk=not args.keep_all, seen=seen)
            for s in add:
                s = dict(s)
                s["key"] = f"{a['id']}_{s.get('key') or site_name(s)}"
                extra.append(s)
            STATS["added_portable"] += [site_name(s) for s in add]
            print(f"   + {a['name']:<10} 可移植站点 {len(add)} 个")
    base_url = chosen["used"] if chosen["used"] != "(本地缓存)" else None

    # ③· 相对路径绝对化 + 资源可达性校验
    print("\n③· 校验站点依赖资源（js / jar / 直播源）")
    candidates = base_sites + extra
    refs = []
    for s in candidates:
        collect_refs(s, refs)
    prefetch_assets(refs, base_url, args.offline)

    fixed, dead = [], []
    for s in candidates:
        s = dict(s)
        fails = []
        nv, _ = fix_paths(s, base_url, args.offline, fails, "")
        crit = [f for f in fails if f[0] in CRITICAL_FIELDS or f[0] == "ext"]
        if crit:
            dead.append(("资源失效", f"{site_name(s)} → {crit[0][1]}"))
            continue
        fixed.append(nv)
    if dead:
        print(f"   已剔除 {len(dead)} 个依赖缺失的站点")
        for r, n in dead[:10]:
            print(f"     - {n}")
    STATS["dropped"] += dead

    # 同名同类型去重（不同源同名站点视为重复）
    uniq, seen_name = [], set()
    for s in fixed:
        sig = (site_name(s), s.get("type"))
        if sig in seen_name:
            STATS["dropped"].append(("同名重复", site_name(s)))
            continue
        seen_name.add(sig)
        uniq.append(s)

    sites = ensure_unique_keys(uniq)
    print(f"   站点合计：{len(sites)}")

    # ④ parses / lives 跨源合并
    print("\n④ 合并解析与直播源")
    raw_parses = [p for a in [chosen] + [x for x in singles if x["id"] != chosen["id"]]
                  for p in (a["data"].get("parses") or [])]
    # 只保留真正的 http 解析器：type=3 且 url 为 "Demo"/"Web" 的是占位符
    raw_parses = [p for p in raw_parses
                  if str(p.get("url", "")).startswith(("http://", "https://"))]
    parses = merge_list(raw_parses, ("url", "name"), limit=args.max_parses)

    raw_lives, live_dead = [], []
    for a in singles:
        for l in (a["data"].get("lives") or []):
            if not isinstance(l, dict):
                continue
            u = str(l.get("url") or "").strip()
            if u.startswith(("http://", "https://")):
                raw_lives.append(l)
            elif u.startswith("."):      # 相对直播源同样需要绝对化
                final, ok = resolve_asset(u, a["used"] if a["used"] != "(本地缓存)" else None,
                                          args.offline)
                if ok:
                    l = dict(l)
                    l["url"] = final
                    raw_lives.append(l)
                else:
                    live_dead.append(str(l.get("name")))
            else:
                live_dead.append(f"{l.get('name')}({u[:20]})")
    lives = merge_list(raw_lives, ("url", "name"), limit=args.max_lives)
    print(f"   parses: {len(parses)}   lives: {len(lives)}")
    if live_dead:
        print(f"   已丢弃无效直播源：{'、'.join(live_dead)}")

    # ⑤ 组装单仓配置
    out = {}
    spider = absolutize_spider(base.get("spider"), base_url)
    if spider:
        out["spider"] = spider
    for k in ("wallpaper", "logo", "danmaku", "headers", "proxy", "hosts", "ijk"):
        if base.get(k):
            v = base[k]
            if k in ("wallpaper", "logo", "danmaku"):
                nv, _ = fix_paths(v, base_url, args.offline, [], k)
                v = nv
            out[k] = v
    out["warningText"] = (
        f"个人自用配置 · 基准源「{chosen['name']}」· 生成于 "
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} · 资源均来自互联网公开分享，仅供学习交流"
    )
    out["sites"] = sites
    if parses:
        out["parses"] = parses
    if lives:
        out["lives"] = lives

    # 合并型字段
    flags = merge_list([f for a in singles for f in (a["data"].get("flags") or [])], ("name",))
    if flags:
        out["flags"] = sorted({str(f) for f in flags})
    ads = merge_list([x for a in singles for x in (a["data"].get("ads") or [])], ("name",), limit=None)
    if ads:
        out["ads"] = ads
    doh = merge_list([x for a in singles for x in (a["data"].get("doh") or [])], ("url", "name"))
    if doh:
        out["doh"] = doh
    rules = merge_list([x for a in singles for x in (a["data"].get("rules") or [])], ("name", "host"))
    if rules:
        out["rules"] = rules

    sp = os.path.join(DIST, "单仓精选.json")
    json.dump(out, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n⑤ 已生成 {sp}")

    # ⑥ 多仓订阅：存活源 + 存活多仓
    # 单仓源只要曾经抓到过（含缓存）就保留在订阅里，避免瞬时抖动把源踢掉
    subs = [{"name": a["name"],
             "url": a["used"] if a["state"] == "online" else (a.get("primary") or a["used"])}
            for a in singles]
    subs += [{"name": a["name"],
              "url": a["used"] if a["state"] == "online" else (a.get("primary") or a["used"])}
             for a in multis]
    mp = os.path.join(DIST, "多仓订阅.json")
    json.dump({"urls": subs}, open(mp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"⑥ 已生成 {mp}（{len(subs)} 个源）")

    # ⑦ 报告
    write_report(alive, chosen, out, STATS, sp, mp)
    write_preview(alive, chosen, out, STATS, os.path.join(DIST, "预览.html"))
    print(f"⑧ 已生成 {os.path.join(DIST, '预览.html')}")
    save_asset_cache()
    return 0


def write_preview(alive, chosen, conf, stats, path):
    """生成可视化预览页（单文件，离线可看）。"""
    sites = conf.get("sites", [])
    rows = []
    for i, s in enumerate(sites, 1):
        api = str(s.get("api", ""))
        if api.startswith("csp_"):
            kind = "Jar 爬虫"
        elif "/js/" in api or api.endswith(".js"):
            kind = "JS 源"
        elif api.startswith("http"):
            kind = "JSON 接口"
        else:
            kind = "其它"
        rows.append({
            "i": i, "name": s.get("name", ""), "key": s.get("key", ""),
            "kind": kind, "type": s.get("type"),
            "search": int(s.get("searchable", 1) or 0),
        })

    src_rows = []
    for a in alive:
        src_rows.append({"name": a["name"], "kind": a["kind"], "n": a["n"],
                         "state": {"online": "在线", "cache": "缓存"}.get(a["state"], a["state"]),
                         "used": a["used"]})

    data = json.dumps({"sites": rows, "sources": src_rows,
                       "stats": {"sites": len(sites), "parses": len(conf.get("parses", [])),
                                 "lives": len(conf.get("lives", [])),
                                 "base": chosen["name"],
                                 "spider": conf.get("spider", "")},
                       "dropped": [n for _, n in stats["dropped"]]},
                      ensure_ascii=False)

    html = PREVIEW_TPL.replace("/*__DATA__*/", "const DATA=" + data + ";")
    open(path, "w", encoding="utf-8").write(html)


PREVIEW_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>自用 TVBox 配置 · 预览</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f1216;color:#e6edf3;font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:32px 20px 60px}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:22px;font-weight:600;letter-spacing:.5px}
.sub{color:#8b949e;font-size:13px;margin-top:6px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:24px 0}
.card{background:#161b22;border:1px solid #232a33;border-radius:10px;padding:16px}
.card .k{color:#8b949e;font-size:12px}
.card .v{font-size:26px;font-weight:600;margin-top:4px;color:#7ee787}
.card .v.s{font-size:15px;color:#e6edf3;word-break:break-all}
h2{font-size:15px;margin:28px 0 12px;color:#e6edf3;font-weight:600}
h2 span{color:#8b949e;font-weight:400;font-size:13px}
table{width:100%;border-collapse:collapse;background:#161b22;border-radius:10px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;font-size:13px;border-bottom:1px solid #1e242c}
th{color:#8b949e;font-weight:500;background:#12171d}
tr:last-child td{border-bottom:0}
.tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;border:1px solid #2d3742;color:#9db3c8}
.ok{color:#7ee787}.warn{color:#f0b232}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
input,select{background:#161b22;border:1px solid #2d3742;color:#e6edf3;border-radius:8px;padding:7px 11px;font-size:13px;outline:none}
input:focus,select:focus{border-color:#3f7cc4}
.count{color:#8b949e;font-size:12px;margin-left:auto}
.tbl-wrap{max-height:520px;overflow:auto;border:1px solid #1e242c;border-radius:10px}
.tbl-wrap table{background:#141920}
.tbl-wrap th{position:sticky;top:0;z-index:1}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#9db3c8}
.foot{margin-top:28px;color:#6e7681;font-size:12px;line-height:1.8}
</style></head><body><div class="wrap">
<h1>自用 TVBox 配置</h1>
<div class="sub" id="sub"></div>
<div class="cards" id="cards"></div>
<h2>配置源状态 <span>本次构建实测</span></h2>
<div class="tbl-wrap" style="max-height:none"><table><thead><tr><th>源</th><th>类型</th><th>条目</th><th>状态</th><th>使用地址</th></tr></thead><tbody id="src"></tbody></table></div>
<h2>站点清单 <span id="siteCount"></span></h2>
<div class="toolbar">
  <input id="q" placeholder="搜索站点名称 / key" style="min-width:220px">
  <select id="kind"><option value="">全部类型</option><option>Jar 爬虫</option><option>JS 源</option><option>JSON 接口</option><option>其它</option></select>
  <span class="count" id="shown"></span>
</div>
<div class="tbl-wrap"><table><thead><tr><th style="width:52px">#</th><th>站点名</th><th>key</th><th>类型</th><th style="width:60px">可搜索</th></tr></thead><tbody id="rows"></tbody></table></div>
<div class="foot" id="foot"></div>
</div>
<script>
/*__DATA__*/
const $=s=>document.querySelector(s);
const S=DATA.stats;
$("#sub").textContent = "基准源「"+S.base+"」· " + S.sites + " 个站点已通过依赖校验";
$("#cards").innerHTML = [
 ["站点", S.sites, 0], ["解析 parses", S.parses, 0], ["直播 lives", S.lives, 0],
 ["基准源", S.base, 1], ["spider", S.spider.split(";")[0].split("/").pop(), 1]
].map(([k,v,small])=>'<div class="card"><div class="k">'+k+'</div><div class="v'+(small?' s':'')+'">'+v+'</div></div>').join("");
$("#src").innerHTML = DATA.sources.map(s=>'<tr><td>'+s.name+'</td><td><span class="tag">'+s.kind+'</span></td><td>'+s.n+'</td><td class="'+(s.state==="在线"?"ok":"warn")+'">'+s.state+'</td><td class="mono">'+s.used+'</td></tr>').join("");
$("#siteCount").textContent = "共 "+DATA.sites.length+" 个";
function render(){
  const q=$("#q").value.trim().toLowerCase(), k=$("#kind").value;
  const list=DATA.sites.filter(s=>(!k||s.kind===k)&&(!q||s.name.toLowerCase().includes(q)||s.key.toLowerCase().includes(q)));
  $("#shown").textContent="显示 "+list.length+" / "+DATA.sites.length;
  $("#rows").innerHTML=list.map(s=>'<tr><td class="mono">'+s.i+'</td><td>'+s.name+'</td><td class="mono">'+s.key+'</td><td><span class="tag">'+s.kind+'</span></td><td>'+(s.search?"✓":"—")+'</td></tr>').join("");
}
$("#q").oninput=render; $("#kind").onchange=render; render();
$("#foot").innerHTML = "已剔除 "+DATA.dropped.length+" 个站点（引流广告、重复、依赖缺失）<br>本配置仅聚合互联网公开分享的接口，仅供学习交流，请勿商用。";
</script></body></html>"""


def write_report(alive, chosen, conf, stats, sp, mp):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L = [f"# 自用 TVBox 配置 · 构建报告\n", f"生成时间：{now}\n"]
    L.append("## 配置源状态\n")
    L.append("| 源 | 类型 | 条目 | 状态 | 使用地址 |")
    L.append("| --- | --- | --- | --- | --- |")
    for a in alive:
        st = {"online": "✅ 在线", "cache": "📦 缓存"}.get(a["state"], a["state"])
        L.append(f"| {a['name']} | {a['kind']} | {a['n']} | {st} | `{a['used']}` |")

    L.append("\n## 单仓精选构成\n")
    L.append(f"- 基准源：**{chosen['name']}**（站点与其 spider 完全匹配）")
    L.append(f"- spider：`{conf.get('spider')}`")
    L.append(f"- 站点：**{len(conf.get('sites', []))}** 个")
    L.append(f"- 解析 parses：{len(conf.get('parses', []))} 个")
    L.append(f"- 直播 lives：{len(conf.get('lives', []))} 个")
    L.append(f"- 跨源并入的可移植站点：{len(stats['added_portable'])} 个")

    if stats["added_portable"]:
        L.append("\n### 跨源并入的站点（type 0/1，不依赖 spider）\n")
        L.append("、".join(stats["added_portable"][:60]))

    if stats["dropped"]:
        L.append(f"\n### 已剔除 {len(stats['dropped'])} 个站点\n")
        from collections import Counter
        c = Counter(r for r, _ in stats["dropped"])
        L.append("、".join(f"{k} {v}" for k, v in c.items()))
        L.append("\n<details><summary>明细</summary>\n")
        for r, n in stats["dropped"][:80]:
            L.append(f"- [{r}] {n}")
        L.append("\n</details>")

    L.append("\n## 说明\n")
    L.append("> 为什么不是把十个源的站点全塞一起？因为 `csp_XXX` 站点必须由**它自己那个源**的 ")
    L.append("> spider（jar/js）来解释。实测各源 csp 类名重合度极低（王二小与其它源交集为 0），")
    L.append("> 强行合并会导致绝大多数站点点开就报错。本配置因此只做「可移植部分的合并」。")
    md = os.path.join(DIST, "配置报告.md")
    open(md, "w", encoding="utf-8").write("\n".join(L))
    print(f"⑦ 已生成 {md}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="指定基准源 id，如 laoliubei / xiaoma")
    ap.add_argument("--offline", action="store_true", help="只用本地缓存")
    ap.add_argument("--keep-all", action="store_true", help="不过滤引流/广告站点")
    ap.add_argument("--no-portable", action="store_true", help="不并入其它源的可移植站点")
    ap.add_argument("--max-parses", type=int, default=30)
    ap.add_argument("--max-lives", type=int, default=15)
    sys.exit(build(ap.parse_args()))
