"""Small filesystem primitives shared by site-data portability stages."""

from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp-{uuid4().hex}')
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
