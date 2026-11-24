# pairwise-audio-device-switch

## 领域

游戏、图形与多媒体

## 主题

音频设备热切换

## 说明

按设备重配、位置连续与单点处理。

初始状态下 `media/audio_device_switch.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
