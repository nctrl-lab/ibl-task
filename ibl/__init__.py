import os

# MKL 2026 + libiomp5md in the conda-forge `ibl` env crashes (0xc06d007f /
# delay-load failure) the first time MKL's Intel-OpenMP thread pool is engaged
# — which happens via numpy.matmul inside PsychoPy's Window._updateDefaultViewMatrix.
# TBB threading avoids that path. Set before any numpy/PsychoPy import.
os.environ.setdefault("MKL_THREADING_LAYER", "TBB")

__version__ = "0.1.0"
