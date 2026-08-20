"""Type stub for the optional compiled extension (native/segmentation.cpp).
Not present unless a C++ toolchain built it - see coredb/signal.py's
try/except ImportError."""

def detect_changepoints_indices(values: list[float], min_size: int, penalty: float) -> list[int]: ...
