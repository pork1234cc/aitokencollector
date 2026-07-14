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

    state = {"claude_total": 0, "codex_total": 0, "at": "--", "rl": None, "err": None}

    # 贴边自动隐藏状态：edge 为贴住的屏幕边（拖动松手时判定），非贴边时为 None
    edge_state = {"edge": None, "hidden": False, "hide_timer": None, "anim_job": None}

    def fmt_k(n):
        if n >= 1_000_000:
            return f"{n / 1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    # ---- 绘制圆形表盘 ----
    def draw():
        canvas.delete("all")
        th = theme()
        c = DIAMETER
        cx = c / 2

        # 未用量轨道环
        canvas.create_oval(MARGIN, MARGIN, c - MARGIN, c - MARGIN,
                            outline=th["track"], width=RING_WIDTH)

        claude_t, codex_t = state["claude_total"], state["codex_total"]
        total = claude_t + codex_t
        bbox = (MARGIN, MARGIN, c - MARGIN, c - MARGIN)
        if total > 0:
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

        # 内部表盘面
        face_inset = MARGIN + RING_WIDTH + 4
        canvas.create_oval(face_inset, face_inset, c - face_inset, c - face_inset,
                            fill=th["face"], outline=th["face"])

        canvas.create_text(cx, cx - 16, text=fmt_k(total), fill=th["fg"],
                            font=("Segoe UI", 15, "bold"))
        canvas.create_text(cx, cx + 6, text=f"C {fmt_k(claude_t)}", fill=th["claude"],
                            font=("Consolas", 9))
        canvas.create_text(cx, cx + 20, text=f"X {fmt_k(codex_t)}", fill=th["codex"],
                            font=("Consolas", 9))
        if state["err"]:
            canvas.create_text(cx, cx + 34, text="⚠ 错误", fill="#ff5555",
                                font=("Segoe UI", 8))

        if edge_state["hidden"] and edge_state["edge"]:
            draw_edge_handle(th, c, cx)

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

    def on_move(e):
        if cfg["locked"]:
            return
        x = root.winfo_x() + e.x - drag["x"]
        y = root.winfo_y() + e.y - drag["y"]
        root.geometry(f"+{x}+{y}")

    def on_release(_):
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
        start_hide_timer()

    canvas.bind("<Enter>", on_canvas_enter)
    canvas.bind("<Leave>", on_canvas_leave)

    # ---- 数据刷新（后台线程扫描，主线程更新 UI）----
    def apply_result(r):
        cs, xs = r["claude_sum"], r["codex_sum"]
        state["claude_total"] = cs["total"]
        state["codex_total"] = xs["total"]
        state["at"] = r["at"]
        state["rl"] = r["codex"]["rate_limit"]
        state["err"] = None
        draw()
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
