#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
token_stats.py — AI Token 统计悬浮窗（小体积单文件方案）

零第三方依赖（托盘功能可选装 pystray + pillow）。
扫描 Codex 与 Claude Code 本地日志，桌面透明悬浮窗展示今日 Token。

用法:
    python token_stats.py                # 启动悬浮窗
    python token_stats.py --nogui        # CLI 模式，打印今日统计
    python token_stats.py --nogui --days 7
    python token_stats.py --nogui --json # JSON 输出
    （测试用）--claude-dir <path> --codex-dir <path>

悬浮窗操作:
    圆形表盘，外环按 Claude/Codex 用量相对占比分色，圆心显示合计 Token 数
    左键拖动        移动窗口（锁定后无效，仅圆盘区域可拖动）
    双击            立即刷新
    右键            菜单：置顶/锁定/透明度/主题/刷新间隔/隐藏(需 pystray)/退出
    位置、透明度、锁定状态、主题自动保存，重启恢复

打包（体积约 15–20MB）:
    pip install pyinstaller
    pyinstaller -F -w --name TokenStats token_stats.py

统计规则（与 DESIGN.md 一致）:
    Claude: sessionId + message.id 去重，冲突保留 total 更大者
    Codex : 同文件内 total_token_usage 非负差分，回退视为新区段
    日期归属按事件 timestamp 的本地时区日期
    只提取用量字段，不保存任何正文
"""
import argparse
import json
import math
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------- 配置 ----------------
CONFIG_PATH = Path.home() / ".token_stats_config.json"
DEFAULT_CONFIG = {
    "x": 100, "y": 100,
    "opacity": 0.88,          # 0.2 ~ 1.0
    "topmost": True,
    "locked": False,
    "refresh_sec": 30,
    "theme": "dark",
    "claude_dir": str(Path.home() / ".claude" / "projects"),
    "codex_dir": str(Path.home() / ".codex" / "sessions"),
}
MIN_OPACITY = 0.2
MAX_OPACITY = 1.0
MIN_REFRESH_SEC = 5
MAX_REFRESH_SEC = 86_400

# ---------------- 圆形表盘主题 ----------------
THEMES = {
    "dark":    {"face": "#1e1e2a", "track": "#33334a", "claude": "#7aa2f7",
                "codex": "#f7a26a", "fg": "#e8e8f0", "dim": "#8a8aa0"},
    "light":   {"face": "#f5f5f7", "track": "#e0e0e6", "claude": "#4361ee",
                "codex": "#e85d75", "fg": "#20202a", "dim": "#70707a"},
    "neon":    {"face": "#0a0a0a", "track": "#1a1a1a", "claude": "#39ff88",
                "codex": "#ff2fb0", "fg": "#ffffff", "dim": "#888888"},
    "minimal": {"face": "#fafafa", "track": "#dcdcdc", "claude": "#3a3a3a",
                "codex": "#7a8fa6", "fg": "#2a2a2a", "dim": "#9a9a9a"},
}
THEME_ORDER = ["dark", "light", "neon", "minimal"]
THEME_NAMES_CN = {"dark": "深色", "light": "浅色", "neon": "霓虹", "minimal": "极简"}

# ---------------- Token 类型明细：圆环细分与面板共用的配色/中文名 ----------------
# 固定配色（跨主题统一，便于一眼区分各成分），顺序即圆环从顶端顺时针的排列
TYPE_KEYS = ["input", "cache_create", "cached_read", "output", "reasoning"]
TYPE_META = {
    "input":        {"cn": "非缓存输入", "color": "#5b8def"},
    "cache_create": {"cn": "缓存创建", "color": "#e0a63b"},
    "cached_read":  {"cn": "缓存读",   "color": "#2bb6a3"},
    "output":       {"cn": "普通输出", "color": "#4caf72"},
    "reasoning":    {"cn": "推理输出", "color": "#9b6dde"},
}


def _number(value, default, minimum, maximum, cast):
    """将配置值限制到安全范围内，无效值回退为默认值。"""
    if isinstance(value, bool):
        return default
    try:
        value = cast(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, value))


def normalize_config(raw):
    """合并并校验配置，确保 GUI 使用的值类型和范围有效。"""
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        cfg.update(raw)

    cfg["x"] = _number(cfg.get("x"), DEFAULT_CONFIG["x"], -100_000, 100_000, int)
    cfg["y"] = _number(cfg.get("y"), DEFAULT_CONFIG["y"], -100_000, 100_000, int)
    cfg["opacity"] = _number(
        cfg.get("opacity"), DEFAULT_CONFIG["opacity"], MIN_OPACITY, MAX_OPACITY, float
    )
    cfg["refresh_sec"] = _number(
        cfg.get("refresh_sec"),
        DEFAULT_CONFIG["refresh_sec"],
        MIN_REFRESH_SEC,
        MAX_REFRESH_SEC,
        int,
    )
    for key in ("topmost", "locked"):
        if not isinstance(cfg.get(key), bool):
            cfg[key] = DEFAULT_CONFIG[key]
    if cfg.get("theme") not in THEMES:
        cfg["theme"] = DEFAULT_CONFIG["theme"]
    for key in ("claude_dir", "codex_dir"):
        value = cfg.get(key)
        if not isinstance(value, str) or not value.strip():
            cfg[key] = DEFAULT_CONFIG[key]
    return cfg


def load_config():
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raw = {}
    return normalize_config(raw)


def save_config(cfg):
    try:
        safe_cfg = normalize_config(cfg)
        CONFIG_PATH.write_text(
            json.dumps(safe_cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (OSError, TypeError, ValueError):
        return False
    return True


# ---------------- 通用工具 ----------------
def local_date(ts: str):
    """ISO 时间串 -> 本地时区 YYYY-MM-DD；无效返回 None"""
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone().strftime("%Y-%m-%d")
    except Exception:
        return None


def date_window(days: int):
    today = datetime.now().date()
    return {(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)}


def nn(v):
    """仅接受非负整数 Token，避免布尔值和异常数值污染统计。"""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def zero_bucket():
    return {"input": 0, "cached_read": 0, "cache_create": 0,
            "output": 0, "reasoning": 0, "total": 0, "events": 0}


def add_bucket(b, d):
    for k in ("input", "cached_read", "cache_create", "output", "reasoning", "total"):
        b[k] += d[k]
    b["events"] += 1


def sub_bucket(b, d):
    for k in ("input", "cached_read", "cache_create", "output", "reasoning", "total"):
        b[k] -= d[k]
    b["events"] -= 1


def component_sum(bucket):
    """汇总互斥展示成分；缓存和推理不得与其父级字段重复相加。"""
    return sum(nn(bucket.get(k)) for k in TYPE_KEYS)


def without_cached_read(bucket):
    """返回不含缓存读取的 Token；缓存创建仍属于本次新处理输入。"""
    return max(0, nn(bucket.get("total")) - nn(bucket.get("cached_read")))


def display_bucket(source, bucket):
    """把来源原始统计转换为可相加的互斥展示成分。"""
    out = zero_bucket()
    out["total"] = nn(bucket.get("total"))
    out["events"] = nn(bucket.get("events"))

    if source == "codex":
        # Codex 的 cached_input_tokens 包含在 input_tokens 内，
        # reasoning_output_tokens 包含在 output_tokens 内，展示时必须扣除父级重叠。
        raw_input = nn(bucket.get("input"))
        raw_output = nn(bucket.get("output"))
        out["cached_read"] = min(nn(bucket.get("cached_read")), raw_input)
        out["reasoning"] = min(nn(bucket.get("reasoning")), raw_output)
        out["input"] = raw_input - out["cached_read"]
        out["output"] = raw_output - out["reasoning"]
        return out

    # Claude Code 的输入、缓存创建、缓存读取、输出是互斥字段。
    for key in TYPE_KEYS:
        out[key] = nn(bucket.get(key))
    return out


def merge_buckets(*buckets):
    """合并已经标准化为互斥成分的多个统计桶。"""
    out = zero_bucket()
    for bucket in buckets:
        for key in out:
            out[key] += nn(bucket.get(key))
    return out


def collect_jsonl(root: str, days: int, name_prefix: str = ""):
    """递归收集 .jsonl，按 mtime 预过滤（窗口 + 1 天缓冲）"""
    out = []
    rootp = Path(root)
    if not rootp.exists():
        return out
    cutoff = datetime.now().timestamp() - (days + 1) * 86400
    for p in rootp.rglob("*.jsonl"):
        try:
            if name_prefix and not p.name.startswith(name_prefix):
                continue
            if p.stat().st_mtime >= cutoff:
                out.append(p)
        except OSError:
            continue
    return out


def iter_jsonl(path: Path):
    """逐行解析，损坏行/尾部半行静默跳过；正文解析后立即丢弃"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except OSError:
        return


# ---------------- Claude Code Adapter ----------------
def scan_claude(root: str, days: int):
    window = date_window(days)
    by_date, by_model = {}, {}
    seen = {}      # dedup_key -> (total, d, date, model)
    warnings = []
    files = collect_jsonl(root, days)

    for f in files:
        for rec in iter_jsonl(f):
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                warnings.append(f"invalid-message {f.name}")
                continue
            usage = msg.get("usage")
            mid = msg.get("id")
            if not isinstance(usage, dict) or not mid:
                continue
            date = local_date(rec.get("timestamp"))
            if date not in window:
                continue

            d = {
                "input": nn(usage.get("input_tokens")),
                "cached_read": nn(usage.get("cache_read_input_tokens")),
                "cache_create": nn(usage.get("cache_creation_input_tokens")),
                "output": nn(usage.get("output_tokens")),
                "reasoning": 0,
            }
            d["total"] = d["input"] + d["cached_read"] + d["cache_create"] + d["output"]

            key = f"{rec.get('sessionId', 'nosess')}::{mid}"
            if key in seen:
                prev_total, prev_d, prev_date, prev_model = seen[key]
                if d["total"] <= prev_total:
                    continue  # 重复行，忽略
                # 同键不同 Usage：冲销旧值，保留更完整的一条
                warnings.append(f"dup-key-conflict {key}: {prev_total} -> {d['total']}")
                if prev_date in by_date:
                    sub_bucket(by_date[prev_date], prev_d)
                if prev_model in by_model:
                    sub_bucket(by_model[prev_model], prev_d)

            model_value = msg.get("model")
            model = model_value if isinstance(model_value, str) and model_value else "unknown"
            seen[key] = (d["total"], d, date, model)
            add_bucket(by_date.setdefault(date, zero_bucket()), d)
            add_bucket(by_model.setdefault(model, zero_bucket()), d)

    return {"by_date": by_date, "by_model": by_model,
            "warnings": warnings, "files": len(files)}


# ---------------- Codex Adapter ----------------
CODEX_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens",
                "reasoning_output_tokens", "total_tokens")


def scan_codex(root: str, days: int):
    window = date_window(days)
    by_date = {}
    warnings = []
    latest_rl = None
    files = collect_jsonl(root, days, name_prefix="rollout-")

    for f in files:
        prev = None  # 差分只在同一文件内进行
        for rec in iter_jsonl(f):
            if not isinstance(rec, dict) or rec.get("type") != "event_msg":
                continue
            payload = rec.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            cum = info.get("total_token_usage")
            if not isinstance(cum, dict) or not cum:
                continue

            ts = rec.get("timestamp") or payload.get("timestamp")
            date = local_date(ts)

            rate_limits = payload.get("rate_limits")
            rl = rate_limits.get("primary") if isinstance(rate_limits, dict) else None
            if (
                isinstance(rl, dict)
                and isinstance(ts, str)
                and (latest_rl is None or ts > latest_rl["ts"])
            ):
                latest_rl = {"used_percent": rl.get("used_percent"),
                             "window_minutes": rl.get("window_minutes"),
                             "resets_at": rl.get("resets_at"), "ts": ts}

            if prev is None:
                delta = {k: nn(cum.get(k)) for k in CODEX_FIELDS}
            else:
                delta, reset = {}, False
                for k in CODEX_FIELDS:
                    dv = nn(cum.get(k)) - nn(prev.get(k))
                    if dv < 0:
                        reset = True
                        break
                    delta[k] = dv
                if reset:
                    warnings.append(f"cumulative-reset {f.name} @ {ts or '?'}")
                    delta = {k: nn(cum.get(k)) for k in CODEX_FIELDS}
            prev = cum

            if date not in window:
                continue
            d = {
                "input": delta["input_tokens"],
                "cached_read": delta["cached_input_tokens"],
                "cache_create": 0,
                "output": delta["output_tokens"],
                "reasoning": delta["reasoning_output_tokens"],
                "total": delta["total_tokens"],
            }
            add_bucket(by_date.setdefault(date, zero_bucket()), d)

    return {"by_date": by_date, "warnings": warnings,
            "rate_limit": latest_rl, "files": len(files)}


# ---------------- 汇总 ----------------
def sum_window(by_date):
    b = zero_bucket()
    for v in by_date.values():
        for k in ("input", "cached_read", "cache_create", "output", "reasoning", "total"):
            b[k] += v[k]
        b["events"] += v["events"]
    return b


def run_scan(cfg, days=1):
    claude = scan_claude(cfg["claude_dir"], days)
    codex = scan_codex(cfg["codex_dir"], days)
    return {
        "claude": claude, "codex": codex,
        "claude_sum": sum_window(claude["by_date"]),
        "codex_sum": sum_window(codex["by_date"]),
        "at": datetime.now().strftime("%H:%M:%S"),
    }


# ---------------- CLI 模式 ----------------
def cli_main(cfg, days, as_json):
    r = run_scan(cfg, days)
    cs, xs = r["claude_sum"], r["codex_sum"]
    if as_json:
        print(json.dumps({
            "window_days": days,
            "claude_code": {"summary": cs, "by_date": r["claude"]["by_date"],
                            "by_model": r["claude"]["by_model"],
                            "files": r["claude"]["files"], "warnings": r["claude"]["warnings"]},
            "codex": {"summary": xs, "by_date": r["codex"]["by_date"],
                      "rate_limit": r["codex"]["rate_limit"],
                      "files": r["codex"]["files"], "warnings": r["codex"]["warnings"]},
            "combined_total": cs["total"] + xs["total"],
        }, ensure_ascii=False, indent=2))
        return

    title = "今日" if days == 1 else f"近 {days} 日"
    fmt = lambda n: f"{n:,}"
    print(f"\n=== AI Token 统计（{title}，本地时区）===\n")

    def row(label, b):
        print(f"{label:<12} 合计 {fmt(b['total']):>12} | 输入 {fmt(b['input']):>11} | "
              f"缓存读 {fmt(b['cached_read']):>11} | 缓存创建 {fmt(b['cache_create']):>9} | "
              f"输出 {fmt(b['output']):>10} | 推理 {fmt(b['reasoning']):>9} | 事件 {b['events']}")

    row("Claude Code", cs)
    row("Codex", xs)
    print("-" * 118)
    comb = zero_bucket()
    for k in comb:
        comb[k] = cs[k] + xs[k]
    row("合计", comb)

    if r["claude"]["by_model"]:
        print("\n--- Claude Code 按模型 ---")
        for m, b in sorted(r["claude"]["by_model"].items(), key=lambda kv: -kv[1]["total"]):
            print(f"  {m:<32} {fmt(b['total']):>12}")
    rl = r["codex"]["rate_limit"]
    if rl:
        print(f"\n--- Codex 额度窗口 ---\n  已用 {rl['used_percent']}% | "
              f"窗口 {rl['window_minutes']} 分钟 | 重置于 {rl['resets_at']}")
    print(f"\n扫描: Claude {r['claude']['files']} 个文件, Codex {r['codex']['files']} 个文件")
    warns = r["claude"]["warnings"] + r["codex"]["warnings"]
    if warns:
        print(f"告警 {len(warns)} 条:")
        for w in warns[:10]:
            print(f"  - {w}")
    print()


# ---------------- 悬浮窗模式 ----------------
def gui_main(cfg):
    import tkinter as tk

    DIAMETER = 150       # 圆形表盘直径
    MARGIN = 6           # 表盘与窗口边缘间距
    RING_WIDTH = 14      # 外环厚度
    KEY_COLOR = "#FE01FE"  # 窗口透明色键（不与任何主题色重复）

    EDGE_THRESHOLD = 20      # 拖动松手时判定"贴边"的距离（像素）
    EDGE_HIDE_DELAY_MS = 1500  # 鼠标离开后延迟多久开始隐藏
    EDGE_VISIBLE_PX = 6      # 隐藏后露出的细边宽度
    EDGE_ANIM_INTERVAL_MS = 15  # 滑动动画帧间隔
    EDGE_ANIM_STEP_PX = 10   # 滑动动画每帧位移

    def theme():
        return THEMES.get(cfg.get("theme", "dark"), THEMES["dark"])

    root = tk.Tk()
    root.overrideredirect(True)                      # 无边框
    root.attributes("-topmost", bool(cfg["topmost"]))
    root.attributes("-alpha", float(cfg["opacity"]))
    try:
        root.attributes("-transparentcolor", KEY_COLOR)  # 仅 Windows 支持，实现真圆形窗口
    except tk.TclError:
        pass
    root.configure(bg=KEY_COLOR)
    root.geometry(f"{DIAMETER}x{DIAMETER}+{int(cfg['x'])}+{int(cfg['y'])}")

    canvas = tk.Canvas(root, width=DIAMETER, height=DIAMETER, bg=KEY_COLOR,
                        highlightthickness=0, bd=0)
    canvas.pack()

    state = {"claude": zero_bucket(), "codex": zero_bucket(),
             "at": "--", "rl": None, "err": None}

    # 悬停细分状态：zone 为当前悬停区域（claude/codex/combined），None 为默认合并视图
    hover = {"zone": None}
    drag_active = {"on": False}
    panel = {"win": None, "canvas": None}

    # 贴边自动隐藏状态：edge 为贴住的屏幕边（拖动松手时判定），非贴边时为 None
    edge_state = {"edge": None, "hidden": False, "hide_timer": None, "anim_job": None}

    def fmt_k(n):
        if n >= 1_000_000:
            return f"{n / 1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    # ---- Token 明细：圆环细分与面板共用 ----
    def type_sum(bucket):
        return component_sum(bucket)

    def bucket_for(zone):
        if zone == "claude":
            return display_bucket("claude", state["claude"])
        if zone == "codex":
            return display_bucket("codex", state["codex"])
        return merge_buckets(
            display_bucket("claude", state["claude"]),
            display_bucket("codex", state["codex"]),
        )

    # ---- 绘制圆形表盘 ----
    def draw():
        canvas.delete("all")
        th = theme()
        c = DIAMETER
        cx = c / 2
        bbox = (MARGIN, MARGIN, c - MARGIN, c - MARGIN)

        # 未用量轨道环
        canvas.create_oval(*bbox, outline=th["track"], width=RING_WIDTH)

        mode = hover["zone"] or "split"
        if mode == "split":
            draw_split_ring(th, bbox)
        else:
            draw_type_ring(bucket_for(mode), bbox)

        # 内部表盘面
        face_inset = MARGIN + RING_WIDTH + 4
        canvas.create_oval(face_inset, face_inset, c - face_inset, c - face_inset,
                            fill=th["face"], outline=th["face"])

        draw_center(th, cx, mode)
        if state["err"]:
            canvas.create_text(cx, cx + 34, text="⚠ 错误", fill="#ff5555",
                                font=("Segoe UI", 8))

        if edge_state["hidden"] and edge_state["edge"]:
            draw_edge_handle(th, c, cx)

    # 默认视图：外环按 Claude / Codex 用量占比分色
    def draw_split_ring(th, bbox):
        claude_t = state["claude"]["total"]
        codex_t = state["codex"]["total"]
        total = claude_t + codex_t
        if total <= 0:
            return
        if codex_t <= 0:
            canvas.create_arc(*bbox, start=90, extent=-359.9,
                               style="arc", outline=th["claude"], width=RING_WIDTH)
        elif claude_t <= 0:
            canvas.create_arc(*bbox, start=90, extent=-359.9,
                               style="arc", outline=th["codex"], width=RING_WIDTH)
        else:
            claude_deg = max(2.0, min(358.0, 360 * claude_t / total))
            codex_deg = 360 - claude_deg
            canvas.create_arc(*bbox, start=90, extent=-claude_deg,
                               style="arc", outline=th["claude"], width=RING_WIDTH)
            canvas.create_arc(*bbox, start=90 - claude_deg, extent=-codex_deg,
                               style="arc", outline=th["codex"], width=RING_WIDTH)

    # 悬停视图：外环按互斥的输入/缓存/输出/推理成分细分
    def draw_type_ring(bucket, bbox):
        total = type_sum(bucket)
        if total <= 0:
            return
        start = 90.0
        for k in TYPE_KEYS:
            v = nn(bucket.get(k))
            if v <= 0:
                continue
            deg = 360 * v / total
            canvas.create_arc(*bbox, start=start, extent=-min(359.9, deg),
                               style="arc", outline=TYPE_META[k]["color"], width=RING_WIDTH)
            start -= deg

    # 圆心文字：只显示含缓存总数；不含缓存读取及分类仅在悬浮面板展示
    def draw_center(th, cx, mode):
        if mode == "split":
            claude_t = state["claude"]["total"]
            codex_t = state["codex"]["total"]
            canvas.create_text(cx, cx - 16, text=fmt_k(claude_t + codex_t), fill=th["fg"],
                                font=("Segoe UI", 15, "bold"))
            canvas.create_text(cx, cx + 6, text=f"C {fmt_k(claude_t)}", fill=th["claude"],
                                font=("Consolas", 9))
            canvas.create_text(cx, cx + 20, text=f"X {fmt_k(codex_t)}", fill=th["codex"],
                                font=("Consolas", 9))
            return
        label = {"claude": "Claude", "codex": "Codex", "combined": "合计"}[mode]
        color = {"claude": th["claude"], "codex": th["codex"], "combined": th["fg"]}[mode]
        bucket = bucket_for(mode)
        canvas.create_text(cx, cx - 10, text=label, fill=color,
                            font=("Segoe UI", 11, "bold"))
        canvas.create_text(cx, cx + 10, text=fmt_k(bucket["total"]), fill=th["fg"],
                            font=("Segoe UI", 14, "bold"))

    # ---- 贴边隐藏时露出的细条把手（保证透明窗口下仍有可悬停区域）----
    def draw_edge_handle(th, c, cx):
        L = EDGE_VISIBLE_PX
        HANDLE_LEN = 40
        bar = th["claude"]
        edge = edge_state["edge"]
        if edge == "left":
            canvas.create_rectangle(c - L, cx - HANDLE_LEN / 2, c, cx + HANDLE_LEN / 2,
                                     fill=bar, outline=bar)
        elif edge == "right":
            canvas.create_rectangle(0, cx - HANDLE_LEN / 2, L, cx + HANDLE_LEN / 2,
                                     fill=bar, outline=bar)
        elif edge == "top":
            canvas.create_rectangle(cx - HANDLE_LEN / 2, c - L, cx + HANDLE_LEN / 2, c,
                                     fill=bar, outline=bar)
        elif edge == "bottom":
            canvas.create_rectangle(cx - HANDLE_LEN / 2, 0, cx + HANDLE_LEN / 2, L,
                                     fill=bar, outline=bar)

    # ---- 拖动（仅圆盘可见区域可触发，透明区域自动穿透）----
    drag = {"x": 0, "y": 0}

    def on_press(e):
        drag["x"], drag["y"] = e.x, e.y
        drag_active["on"] = True
        if hover["zone"] is not None:
            hover["zone"] = None
            hide_panel()
            draw()

    def on_move(e):
        if cfg["locked"]:
            return
        x = root.winfo_x() + e.x - drag["x"]
        y = root.winfo_y() + e.y - drag["y"]
        root.geometry(f"+{x}+{y}")

    def on_release(_):
        drag_active["on"] = False
        cfg["x"], cfg["y"] = root.winfo_x(), root.winfo_y()
        save_config(cfg)
        cancel_hide_timer()
        cancel_anim()
        edge_state["hidden"] = False
        edge_state["edge"] = detect_edge()
        draw()

    canvas.bind("<Button-1>", on_press)
    canvas.bind("<B1-Motion>", on_move)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<Double-Button-1>", lambda e: refresh_now())

    # ---- 贴边自动隐藏：拖到屏幕边缘松手后吸附，鼠标离开延迟滑出，移入再滑回 ----
    def detect_edge():
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        x, y = root.winfo_x(), root.winfo_y()
        if x <= EDGE_THRESHOLD:
            return "left"
        if x + DIAMETER >= sw - EDGE_THRESHOLD:
            return "right"
        if y <= EDGE_THRESHOLD:
            return "top"
        if y + DIAMETER >= sh - EDGE_THRESHOLD:
            return "bottom"
        return None

    def cancel_hide_timer():
        if edge_state["hide_timer"] is not None:
            root.after_cancel(edge_state["hide_timer"])
            edge_state["hide_timer"] = None

    def cancel_anim():
        if edge_state["anim_job"] is not None:
            root.after_cancel(edge_state["anim_job"])
            edge_state["anim_job"] = None

    def slide_to(target_x, target_y, on_done=None):
        cancel_anim()

        def step():
            cur_x, cur_y = root.winfo_x(), root.winfo_y()
            dx, dy = target_x - cur_x, target_y - cur_y
            if abs(dx) <= EDGE_ANIM_STEP_PX and abs(dy) <= EDGE_ANIM_STEP_PX:
                root.geometry(f"+{target_x}+{target_y}")
                edge_state["anim_job"] = None
                if on_done:
                    on_done()
                return
            nx = cur_x + max(-EDGE_ANIM_STEP_PX, min(EDGE_ANIM_STEP_PX, dx))
            ny = cur_y + max(-EDGE_ANIM_STEP_PX, min(EDGE_ANIM_STEP_PX, dy))
            root.geometry(f"+{nx}+{ny}")
            edge_state["anim_job"] = root.after(EDGE_ANIM_INTERVAL_MS, step)

        step()

    def begin_hide():
        edge_state["hide_timer"] = None
        edge = edge_state["edge"]
        if not edge or edge_state["hidden"]:
            return
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        x, y = root.winfo_x(), root.winfo_y()
        offset = DIAMETER - EDGE_VISIBLE_PX
        target = {
            "left": (-offset, y),
            "right": (sw - EDGE_VISIBLE_PX, y),
            "top": (x, -offset),
            "bottom": (x, sh - EDGE_VISIBLE_PX),
        }[edge]

        def done():
            edge_state["hidden"] = True
            draw()

        slide_to(*target, on_done=done)

    def begin_show():
        edge_state["hidden"] = False
        draw()
        slide_to(int(cfg["x"]), int(cfg["y"]))

    def start_hide_timer():
        if not edge_state["edge"] or edge_state["hidden"]:
            return
        cancel_hide_timer()
        edge_state["hide_timer"] = root.after(EDGE_HIDE_DELAY_MS, begin_hide)

    def on_canvas_enter(_):
        cancel_hide_timer()
        if edge_state["hidden"]:
            begin_show()

    def on_canvas_leave(_):
        if hover["zone"] is not None:
            hover["zone"] = None
            hide_panel()
            draw()
        start_hide_timer()

    canvas.bind("<Enter>", on_canvas_enter)
    canvas.bind("<Leave>", on_canvas_leave)

    # ---- 悬停细分：移到某段圆环即弹出该来源/成分的明细面板 ----
    def zone_at(ex, ey):
        cx = DIAMETER / 2
        dx, dy = ex - cx, ey - cx
        r = math.hypot(dx, dy)
        face_inset = MARGIN + RING_WIDTH + 4
        inner_r = (DIAMETER - 2 * face_inset) / 2
        outer_r = (DIAMETER - 2 * MARGIN) / 2 + RING_WIDTH / 2
        if r > outer_r:
            return None                       # 圆形之外（透明区）
        if r <= inner_r:
            return "combined"                 # 圆心 → 合并成分视图
        claude_t = state["claude"]["total"]
        codex_t = state["codex"]["total"]
        total = claude_t + codex_t
        if total <= 0:
            return "combined"
        if codex_t <= 0:
            return "claude"
        if claude_t <= 0:
            return "codex"
        ang = math.degrees(math.atan2(-dy, dx)) % 360   # 0=右, 90=上
        claude_deg = max(2.0, min(358.0, 360 * claude_t / total))
        delta = (90 - ang) % 360              # 自顶端顺时针的角度
        return "claude" if delta <= claude_deg else "codex"

    def on_motion(e):
        if drag_active["on"] or edge_state["hidden"] or edge_state["anim_job"] is not None:
            return
        z = zone_at(e.x, e.y)
        if z == hover["zone"]:
            return
        hover["zone"] = z
        draw()
        if z is None:
            hide_panel()
        else:
            show_panel(z)

    canvas.bind("<Motion>", on_motion)

    # ---- 明细面板（独立无边框窗口，作为悬停浮层）----
    def ensure_panel():
        if panel["win"] is not None:
            return
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        pcv = tk.Canvas(win, highlightthickness=0, bd=0)
        pcv.pack()
        panel["win"], panel["canvas"] = win, pcv
        win.withdraw()

    def hide_panel():
        if panel["win"] is not None:
            panel["win"].withdraw()

    def place_panel(W, H):
        root.update_idletasks()
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        gap = 10
        x = rx + DIAMETER + gap
        if x + W > sw:                        # 右侧放不下就贴左侧
            x = rx - W - gap
        x = max(0, min(x, sw - W))
        y = ry + DIAMETER // 2 - H // 2
        y = max(0, min(y, sh - H))
        panel["win"].geometry(f"{W}x{H}+{x}+{y}")

    def show_panel(zone):
        ensure_panel()
        th = theme()
        bucket = bucket_for(zone)
        label = {"claude": "Claude Code", "codex": "Codex", "combined": "全部合计"}[zone]
        head_color = {"claude": th["claude"], "codex": th["codex"], "combined": th["fg"]}[zone]

        types = [k for k in TYPE_KEYS if nn(bucket.get(k)) > 0]
        denom = type_sum(bucket) or 1

        W, pad = 246, 14
        header_h, row_h, foot_h = 64, 30, 62
        H = header_h + row_h * max(1, len(types)) + foot_h

        pcv = panel["canvas"]
        pcv.config(width=W, height=H, bg=th["face"])
        pcv.delete("all")
        pcv.create_rectangle(1, 1, W - 1, H - 1, outline=th["track"], width=1)

        pcv.create_text(pad, 16, text=label, anchor="w", fill=head_color,
                        font=("Segoe UI", 12, "bold"))
        pcv.create_text(pad, 38, text="含缓存总数", anchor="w", fill=th["dim"],
                        font=("Segoe UI", 9))
        pcv.create_text(W - pad, 38, text=f"{bucket['total']:,}", anchor="e",
                        fill=th["fg"], font=("Consolas", 10, "bold"))
        pcv.create_text(pad, 54, text="不含缓存读取", anchor="w", fill=th["dim"],
                        font=("Segoe UI", 9))
        pcv.create_text(W - pad, 54, text=f"{without_cached_read(bucket):,}", anchor="e",
                        fill=th["fg"], font=("Consolas", 10))

        y = header_h
        bar_w = W - 2 * pad
        if not types:
            pcv.create_text(W / 2, y + row_h / 2, text="暂无数据", fill=th["dim"],
                            font=("Segoe UI", 10))
        for k in types:
            v = nn(bucket.get(k))
            pct = v / denom
            color = TYPE_META[k]["color"]
            pcv.create_rectangle(pad, y + 4, pad + 10, y + 14, fill=color, outline=color)
            pcv.create_text(pad + 18, y + 9,
                            text=f"{TYPE_META[k]['cn']} {pct * 100:.0f}%", anchor="w",
                            fill=th["fg"], font=("Segoe UI", 10))
            pcv.create_text(W - pad, y + 9, text=f"{v:,}", anchor="e",
                            fill=th["fg"], font=("Consolas", 10))
            # 占比细条：一眼看出各成分占比
            pcv.create_rectangle(pad, y + 19, pad + bar_w, y + 22,
                                 fill=th["track"], outline=th["track"])
            pcv.create_rectangle(pad, y + 19, pad + bar_w * pct, y + 22,
                                 fill=color, outline=color)
            y += row_h

        # ---- 底部分析 ----
        inp = nn(bucket.get("input"))
        cr = nn(bucket.get("cached_read"))
        cc = nn(bucket.get("cache_create"))
        events = nn(bucket.get("events"))
        in_side = inp + cr + cc
        hit = cr / in_side if in_side > 0 else 0
        avg = nn(bucket.get("total")) / events if events > 0 else 0

        pcv.create_line(pad, y + 6, W - pad, y + 6, fill=th["track"])
        pcv.create_text(pad, y + 22, anchor="w", fill=th["dim"], font=("Segoe UI", 9),
                        text=f"缓存命中率 {hit * 100:.0f}%  ·  缓存读省下 {fmt_k(cr)}")
        pcv.create_text(pad, y + 40, anchor="w", fill=th["dim"], font=("Segoe UI", 9),
                        text=f"事件 {events} 次  ·  均次 {fmt_k(int(avg))}")

        place_panel(W, H)
        panel["win"].deiconify()
        panel["win"].lift()

    # ---- 数据刷新（后台线程扫描，主线程更新 UI）----
    def apply_result(r):
        state["claude"] = r["claude_sum"]
        state["codex"] = r["codex_sum"]
        state["at"] = r["at"]
        state["rl"] = r["codex"]["rate_limit"]
        state["err"] = None
        draw()
        if hover["zone"]:
            show_panel(hover["zone"])
        build_menu()

    scanning = {"on": False}

    def refresh_now():
        if scanning["on"]:
            return
        scanning["on"] = True

        def work():
            try:
                r = run_scan(cfg, days=1)
                root.after(0, apply_result, r)
            except Exception as ex:
                def set_err(msg=str(ex)):
                    state["err"] = msg
                    draw()
                    build_menu()
                root.after(0, set_err)
            finally:
                scanning["on"] = False

        threading.Thread(target=work, daemon=True).start()

    def schedule():
        refresh_now()
        root.after(max(5, int(cfg["refresh_sec"])) * 1000, schedule)

    # ---- 右键菜单 ----
    menu = tk.Menu(root, tearoff=0)

    def toggle_topmost():
        cfg["topmost"] = not cfg["topmost"]
        root.attributes("-topmost", cfg["topmost"])
        save_config(cfg)
        build_menu()

    def toggle_locked():
        cfg["locked"] = not cfg["locked"]
        save_config(cfg)
        build_menu()

    def set_opacity(v):
        cfg["opacity"] = v
        root.attributes("-alpha", v)
        save_config(cfg)

    def set_refresh(sec):
        cfg["refresh_sec"] = sec
        save_config(cfg)

    def set_theme(name):
        cfg["theme"] = name
        save_config(cfg)
        draw()
        build_menu()

    def quit_app():
        save_config(cfg)
        root.destroy()

    # 托盘（可选）：pip install pystray pillow
    tray_ok = False
    try:
        import pystray
        from PIL import Image, ImageDraw
        tray_ok = True
    except ImportError:
        pass

    tray_ref = {"icon": None}

    def hide_to_tray():
        root.withdraw()
        if tray_ref["icon"] is None:
            img = Image.new("RGB", (64, 64), "#1e1e2a")
            dr = ImageDraw.Draw(img)
            dr.rectangle([12, 28, 52, 36], fill="#7aa2f7")
            dr.rectangle([28, 12, 36, 52], fill="#7aa2f7")

            def show(icon, item):
                root.after(0, root.deiconify)

            def tray_quit(icon, item):
                icon.stop()
                root.after(0, quit_app)

            icon = pystray.Icon("token_stats", img, "AI Token 统计",
                                menu=pystray.Menu(
                                    pystray.MenuItem("显示", show, default=True),
                                    pystray.MenuItem("退出", tray_quit)))
            tray_ref["icon"] = icon
            threading.Thread(target=icon.run, daemon=True).start()

    def build_menu():
        menu.delete(0, "end")
        if state["err"]:
            status_text = f"错误: {state['err']}"
        else:
            status_text = f"更新 {state['at']}"
            rl = state["rl"]
            if rl and rl.get("used_percent") is not None:
                status_text += f" · 额度 {rl['used_percent']}%"
        menu.add_command(label=status_text, state="disabled")
        menu.add_command(label="立即刷新", command=refresh_now)
        menu.add_separator()
        menu.add_command(label=("✓ 置顶" if cfg["topmost"] else "  置顶"), command=toggle_topmost)
        menu.add_command(label=("✓ 锁定位置" if cfg["locked"] else "  锁定位置"), command=toggle_locked)
        th_menu = tk.Menu(menu, tearoff=0)
        for key in THEME_ORDER:
            mark = "✓ " if cfg.get("theme") == key else "  "
            th_menu.add_command(label=mark + THEME_NAMES_CN[key], command=lambda k=key: set_theme(k))
        menu.add_cascade(label="主题", menu=th_menu)
        op = tk.Menu(menu, tearoff=0)
        for pct in (100, 90, 80, 70, 60, 50, 40, 30, 20):
            op.add_command(label=f"{pct}%", command=lambda v=pct / 100: set_opacity(v))
        menu.add_cascade(label="透明度", menu=op)
        rf = tk.Menu(menu, tearoff=0)
        for sec in (10, 30, 60, 300):
            rf.add_command(label=f"{sec} 秒", command=lambda s=sec: set_refresh(s))
        menu.add_cascade(label="刷新间隔", menu=rf)
        menu.add_separator()
        if tray_ok:
            menu.add_command(label="隐藏到托盘", command=hide_to_tray)
        else:
            menu.add_command(label="隐藏到托盘（需安装 pystray）", state="disabled")
        menu.add_command(label="退出", command=quit_app)

    build_menu()
    canvas.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    draw()
    schedule()
    root.mainloop()


# ---------------- 入口 ----------------
def positive_int(value):
    """解析大于零的整数参数，不合法时交由 argparse 显示错误。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError) as ex:
        raise argparse.ArgumentTypeError("必须是正整数") from ex
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def main():
    ap = argparse.ArgumentParser(description="AI Token 统计（Codex + Claude Code）")
    ap.add_argument("--nogui", action="store_true", help="CLI 模式")
    ap.add_argument("--days", type=positive_int, default=1, help="统计天数（含今日）")
    ap.add_argument("--json", action="store_true", help="JSON 输出（配合 --nogui）")
    ap.add_argument("--claude-dir", default=None)
    ap.add_argument("--codex-dir", default=None)
    args = ap.parse_args()

    if args.json and not args.nogui:
        ap.error("--json 只能与 --nogui 一起使用")

    cfg = load_config()
    if args.claude_dir:
        cfg["claude_dir"] = args.claude_dir
    if args.codex_dir:
        cfg["codex_dir"] = args.codex_dir

    if args.nogui:
        cli_main(cfg, args.days, args.json)
    else:
        gui_main(cfg)


if __name__ == "__main__":
    main()
