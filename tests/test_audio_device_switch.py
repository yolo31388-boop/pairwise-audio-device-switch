import pytest
import media.audio_device_switch as mod

CASES = [
    ("reconfigure_by_rate", (1,)),
    ("stop_old_stream", (1,)),
    ("carry_position", (1,)),
    ("rebuild_buffer", (1,)),
    ("single_handler", (1,)),
    ("crossfade_switch", (1,)),
    ("open", (1,)),
    ("switch", (1,)),
    ("close", (1,)),
    ("write", (1,)),
    ("validate_device", (1,)),
    ("metrics", (1,)),
    ("audit", (1,)),
    ("fallback", (1,)),
    ("determinism", (1,)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
