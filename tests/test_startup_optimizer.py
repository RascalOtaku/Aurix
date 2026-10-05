"""Tests for src.startup_optimizer.detect_resource_mode.

The function must never raise (its contract) and must map RAM/GPU to the
documented tiers. psutil/torch are stubbed via sys.modules so the tests run
without real hardware or optional dependencies.
"""
import sys
import types
from unittest import mock

import pytest

from src import startup_optimizer


def _fake_psutil(total_gb):
    mod = types.ModuleType("psutil")
    vm = mock.Mock()
    vm.total = total_gb * (1024 ** 3)
    mod.virtual_memory = mock.Mock(return_value=vm)
    return mod


def _fake_torch(cuda_available):
    mod = types.ModuleType("torch")
    cuda = mock.Mock()
    cuda.is_available = mock.Mock(return_value=cuda_available)
    mod.cuda = cuda
    return mod


def _detect(total_gb=None, cuda=False, psutil_ok=True, torch_ok=True):
    """Run detect_resource_mode with stubbed (or missing) psutil/torch.

    sys.modules[name] = None makes `import name` raise ImportError.
    """
    overrides = {
        "psutil": _fake_psutil(total_gb) if psutil_ok else None,
        "torch": _fake_torch(cuda) if torch_ok else None,
    }
    with mock.patch.dict(sys.modules, overrides):
        return startup_optimizer.detect_resource_mode()


def test_ultra_low():
    assert _detect(2) == "ultra-low"


def test_low():
    assert _detect(6) == "low"


def test_normal():
    assert _detect(12) == "normal"


def test_gpu_ignored_below_16gb():
    # Per the docstring, 'gpu' requires >=16GB; a GPU on a smaller box stays 'normal'.
    assert _detect(12, cuda=True) == "normal"


def test_gpu():
    assert _detect(32, cuda=True) == "gpu"


def test_high():
    assert _detect(32, cuda=False) == "high"


def test_high_when_torch_missing():
    assert _detect(32, torch_ok=False) == "high"


def test_psutil_missing_defaults_to_normal_without_raising():
    # The function's contract is "never raises".
    assert _detect(psutil_ok=False) == "normal"


def test_boundary_4gb_is_low():
    assert _detect(4.0) == "low"


def test_boundary_8gb_is_normal():
    assert _detect(8.0) == "normal"


def test_boundary_16gb_no_gpu_is_high():
    assert _detect(16.0, cuda=False) == "high"


def test_boundary_16gb_with_gpu_is_gpu():
    assert _detect(16.0, cuda=True) == "gpu"
