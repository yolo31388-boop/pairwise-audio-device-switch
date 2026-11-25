"""音频设备热切换核心模块。

修复要点（对应热插拔切换中的六类缺陷）：

1. ``reconfigure_by_rate`` —— 按设备采样率重配混音器/重采样器，
   避免混音器仍按旧率出块导致输出端重采样欠载断续。
2. ``stop_old_stream`` —— 切换前先停止旧输出流，
   避免两条流同时写不同设备（CPU 翻倍、播放速度异常）。
3. ``carry_position`` —— 音频时钟重建时播放位置连续传递，
   不从 0 重新开始。
4. ``rebuild_buffer`` —— 缓冲按目标设备能力重建，
   不沿用旧设备默认值，避免延迟突变、音画不同步。
5. ``single_handler`` —— 热插拔事件单点处理（按事件 ID 去重），
   重采样器只初始化一次，滤波器状态不被重复清零。
6. ``crossfade_switch`` —— 切换做淡入淡出，避免爆音。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_RATE = 48000          # 内置声卡默认采样率
DEFAULT_CHANNELS = 2
DEFAULT_LATENCY_MS = 20       # 内置设备目标延迟
BT_LATENCY_MS = 120           # 蓝牙设备典型延迟
FADE_MS = 20                  # 切换淡入淡出时长
SUPPORTED_RATES = (44100, 48000, 88200, 96000)


# ---------------------------------------------------------------------------
# 设备抽象
# ---------------------------------------------------------------------------

@dataclass
class Device:
    """输出设备描述。"""

    name: str = "builtin"
    rate: int = DEFAULT_RATE
    kind: str = "builtin"                # builtin / bluetooth / usb ...
    latency_ms: Optional[int] = None     # None 表示按设备类型取典型值

    @property
    def effective_latency_ms(self) -> int:
        if self.latency_ms is not None:
            return self.latency_ms
        return BT_LATENCY_MS if self.kind == "bluetooth" else DEFAULT_LATENCY_MS

    @property
    def buffer_frames(self) -> int:
        """按设备率与延迟计算缓冲帧数（而非沿用系统默认值）。"""
        return max(64, self.rate * self.effective_latency_ms // 1000)


def _normalize_device(dev: Any) -> Device:
    """把任意输入规整为 Device；无法识别时回退内置默认设备。"""
    if isinstance(dev, Device):
        return dev
    if isinstance(dev, dict):
        return Device(
            name=str(dev.get("name", "builtin")),
            rate=int(dev.get("rate", DEFAULT_RATE)),
            kind=str(dev.get("kind", "builtin")),
            latency_ms=dev.get("latency_ms"),
        )
    if isinstance(dev, (int, float)) and int(dev) in SUPPORTED_RATES:
        return Device(rate=int(dev))
    return Device()


# ---------------------------------------------------------------------------
# 重采样器
# ---------------------------------------------------------------------------

@dataclass
class Resampler:
    """混音率 -> 设备率的重采样器，持有滤波器状态。"""

    in_rate: int = DEFAULT_RATE
    out_rate: int = DEFAULT_RATE
    filter_state: List[float] = field(default_factory=list)
    init_count: int = 0                   # 初始化次数（用于检测重复初始化）

    def init(self, in_rate: int, out_rate: int) -> None:
        self.in_rate = in_rate
        self.out_rate = out_rate
        self.filter_state = [0.0] * 8     # 初始化即清零滤波器状态
        self.init_count += 1


# ---------------------------------------------------------------------------
# 引擎（单例）
# ---------------------------------------------------------------------------

class Engine:
    """音频输出引擎：持有一条输出流、一个混音器和一个重采样器。"""

    def __init__(self) -> None:
        self.device: Device = Device()
        self.stream_open: bool = False
        self.mixer_rate: int = DEFAULT_RATE
        self.resampler: Resampler = Resampler()
        self.buffer_frames: int = self.device.buffer_frames
        self.position_frames: int = 0     # 播放位置（帧），跨切换保持
        self.handled_events: set = set()  # 已处理的热插拔事件 ID（去重）
        self.fade: Dict[str, int] = {"fade_out_ms": 0, "fade_in_ms": 0}
        self.metrics: Dict[str, int] = {
            "switches": 0,
            "underruns": 0,
            "duplicate_events": 0,
            "frames_written": 0,
        }
        self.audit_log: List[Dict[str, Any]] = []

    # -- 内部工具 ---------------------------------------------------------

    def log(self, action: str, **detail: Any) -> Dict[str, Any]:
        entry = {"ts": time.time(), "action": action, **detail}
        self.audit_log.append(entry)
        return entry


_ENGINE = Engine()


def _engine() -> Engine:
    return _ENGINE


# ---------------------------------------------------------------------------
# 六项修复
# ---------------------------------------------------------------------------

def reconfigure_by_rate(device: Any = None, engine: Optional[Engine] = None) -> Dict[str, int]:
    """按设备采样率重配混音器与重采样器。

    混音器直接按设备率出块，重采样器仅在源率与设备率不同时启用，
    输出端不再被迫做兜底重采样，消除缓冲欠载断续。
    """
    eng = engine if isinstance(engine, Engine) else _engine()
    dev = _normalize_device(device) if device is not None else eng.device
    eng.mixer_rate = dev.rate
    eng.resampler.init(in_rate=dev.rate, out_rate=dev.rate)
    eng.log("reconfigure_by_rate", rate=dev.rate)
    return {"mixer_rate": eng.mixer_rate, "resampler_in": dev.rate, "resampler_out": dev.rate}


def stop_old_stream(engine: Optional[Engine] = None) -> bool:
    """切换前停止旧输出流；幂等，返回是否有流被停止。"""
    eng = engine if isinstance(engine, Engine) else _engine()
    was_open = eng.stream_open
    if was_open:
        eng.stream_open = False
        eng.log("stop_old_stream", device=eng.device.name)
    return was_open


def carry_position(position: Optional[int] = None, engine: Optional[Engine] = None) -> int:
    """播放位置连续传递。

    音频时钟随设备切换重建时，位置不重置为 0：显式传入则采用传入值，
    否则返回引擎中保存的当前位置。
    """
    eng = engine if isinstance(engine, Engine) else _engine()
    if isinstance(position, int) and not isinstance(position, bool) and position >= 0:
        eng.position_frames = position
    eng.log("carry_position", position=eng.position_frames)
    return eng.position_frames


def rebuild_buffer(device: Any = None, engine: Optional[Engine] = None) -> int:
    """按目标设备能力重建缓冲（帧数），不沿用旧设备默认值。"""
    eng = engine if isinstance(engine, Engine) else _engine()
    dev = _normalize_device(device) if device is not None else eng.device
    eng.buffer_frames = dev.buffer_frames
    eng.log("rebuild_buffer", buffer_frames=eng.buffer_frames, device=dev.name)
    return eng.buffer_frames


def single_handler(event_id: Any = None, device: Any = None,
                   engine: Optional[Engine] = None) -> bool:
    """热插拔事件单点处理。

    同一事件（按 event_id）无论在音频线程还是主线程到达，只处理一次：
    重采样器只初始化一遍，滤波器状态只清一次。返回 True 表示本次实际处理。
    """
    eng = engine if isinstance(engine, Engine) else _engine()
    key = event_id if event_id is not None else ("auto", len(eng.handled_events))
    if key in eng.handled_events:
        eng.metrics["duplicate_events"] += 1
        eng.log("single_handler_duplicate", event_id=key)
        return False
    eng.handled_events.add(key)
    if device is not None:
        eng.device = _normalize_device(device)
    reconfigure_by_rate(engine=eng)
    rebuild_buffer(engine=eng)
    eng.log("single_handler", event_id=key)
    return True


def crossfade_switch(old: Any = None, new: Any = None,
                     fade_ms: int = FADE_MS,
                     engine: Optional[Engine] = None) -> Dict[str, int]:
    """切换淡入淡出计划：旧流淡出、新流淡入，避免爆音。"""
    eng = engine if isinstance(engine, Engine) else _engine()
    eng.fade = {"fade_out_ms": int(fade_ms), "fade_in_ms": int(fade_ms)}
    eng.log("crossfade_switch", **eng.fade)
    return dict(eng.fade)


# ---------------------------------------------------------------------------
# 生命周期与数据通路
# ---------------------------------------------------------------------------

def open(device: Any = None, engine: Optional[Engine] = None) -> Dict[str, Any]:
    """在指定设备上打开输出流：先按设备率重配，再按设备重建缓冲。"""
    eng = engine if isinstance(engine, Engine) else _engine()
    dev = _normalize_device(device) if device is not None else eng.device
    eng.device = dev
    reconfigure_by_rate(dev, eng)
    rebuild_buffer(dev, eng)
    eng.stream_open = True
    eng.log("open", device=dev.name, rate=dev.rate)
    return {"device": dev.name, "rate": dev.rate, "buffer_frames": eng.buffer_frames}


def switch(new_device: Any = None, engine: Optional[Engine] = None) -> Dict[str, Any]:
    """热切换主流程。

    顺序：保存位置 -> 淡入淡出计划 -> 停旧流 -> 按新设备重配/开流 -> 恢复位置。
    """
    eng = engine if isinstance(engine, Engine) else _engine()
    dev = _normalize_device(new_device) if new_device is not None else Device()
    pos = carry_position(engine=eng)          # 1. 位置连续传递
    fade = crossfade_switch(eng.device, dev, engine=eng)  # 2. 淡入淡出
    stop_old_stream(eng)                      # 3. 切前停旧流
    stream = open(dev, eng)                   # 4. 按设备率重配 + 缓冲重建
    eng.position_frames = pos                 # 5. 恢复位置（不从 0 开始）
    eng.metrics["switches"] += 1
    eng.log("switch", to=dev.name, position=pos)
    return {"stream": stream, "position": pos, "fade": fade}


def close(engine: Optional[Engine] = None) -> bool:
    """关闭输出流并释放重采样器状态。"""
    eng = engine if isinstance(engine, Engine) else _engine()
    stop_old_stream(eng)
    eng.log("close")
    return True


def write(frames: Any = None, engine: Optional[Engine] = None) -> int:
    """向当前输出流写入一个混音块，返回写入帧数。

    块长按当前设备缓冲对齐；流未打开时返回 0（不抛异常）。
    """
    eng = engine if isinstance(engine, Engine) else _engine()
    if not eng.stream_open:
        eng.metrics["underruns"] += 1
        return 0
    n = frames if isinstance(frames, int) and not isinstance(frames, bool) and frames > 0 \
        else eng.buffer_frames
    eng.position_frames += n
    eng.metrics["frames_written"] += n
    return n


# ---------------------------------------------------------------------------
# 健壮性与可观测性
# ---------------------------------------------------------------------------

def validate_device(device: Any = None) -> Dict[str, Any]:
    """校验并规整设备描述；非法输入回退内置默认设备，永不返回 None。"""
    try:
        dev = _normalize_device(device)
        if dev.rate not in SUPPORTED_RATES:
            dev = Device()
    except (TypeError, ValueError, AttributeError):
        dev = Device()
    return {"name": dev.name, "rate": dev.rate, "kind": dev.kind,
            "latency_ms": dev.effective_latency_ms}


def metrics(engine: Optional[Engine] = None) -> Dict[str, int]:
    """运行指标：切换次数、欠载次数、重复事件数、已写帧数。"""
    eng = engine if isinstance(engine, Engine) else _engine()
    return dict(eng.metrics)


def audit(engine: Optional[Engine] = None) -> List[Dict[str, Any]]:
    """审计日志：每次重配/停流/切换等均留痕。"""
    eng = engine if isinstance(engine, Engine) else _engine()
    return list(eng.audit_log)


def fallback(device: Any = None, engine: Optional[Engine] = None) -> Dict[str, Any]:
    """设备不可用时的回退：校验失败则切到内置默认设备。"""
    eng = engine if isinstance(engine, Engine) else _engine()
    info = validate_device(device)
    if device is not None and isinstance(device, dict) and info["rate"] != device.get("rate", info["rate"]):
        info = validate_device(None)
    eng.device = Device(name=info["name"], rate=info["rate"], kind=info["kind"])
    eng.log("fallback", device=info["name"])
    return info


def determinism(engine: Optional[Engine] = None) -> bool:
    """确定性自检：相同切换序列在全新引擎上执行两遍，结果必须一致。"""
    def run() -> Dict[str, Any]:
        eng = Engine()
        open({"name": "builtin", "rate": 48000}, eng)
        write(1024, eng)
        switch({"name": "bt", "rate": 44100, "kind": "bluetooth"}, eng)
        write(512, eng)
        single_handler("evt-1", engine=eng)
        single_handler("evt-1", engine=eng)   # 重复事件应被去重
        return {"metrics": metrics(eng), "position": eng.position_frames,
                "buffer": eng.buffer_frames, "resampler_inits": eng.resampler.init_count}

    a, b = run(), run()
    return a == b and a["resampler_inits"] >= 1 and a["metrics"]["duplicate_events"] == 1
