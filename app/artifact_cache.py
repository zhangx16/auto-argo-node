"""Small metadata-keyed digest cache; frozen remote verification remains authoritative."""
from functools import lru_cache
import hashlib
from pathlib import Path


@lru_cache(maxsize=64)
def _digest(path,size,mtime,ctime):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(path):
    path=Path(path);stat=path.stat()
    return _digest(str(path),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
