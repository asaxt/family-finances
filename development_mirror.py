"""Create an encrypted development snapshot without opening production for writes."""
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from vault import MAGIC


def prepare_snapshot(source_directory, snapshot_directory):
    source = Path(source_directory).resolve(strict=True)
    destination = Path(snapshot_directory).resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Production and mirror storage must be separate directories.")
    vault_path, auth_path = source / 'family-finances.vault', source / '.auth.json'
    # Both production files are atomically replaced by the app. Re-read the pair
    # to detect a sync/password change during copying; never stop production.
    for _ in range(3):
        auth = auth_path.read_bytes()
        vault = vault_path.read_bytes()
        if auth == auth_path.read_bytes() and vault == vault_path.read_bytes():
            break
    else:
        raise ValueError("Production changed while copying. Please relaunch development.")
    if not vault.startswith(MAGIC):
        raise ValueError("The production source must be an encrypted vault.")
    config = json.loads(auth)
    if not all(config.get(key) for key in ('password_hash', 'vault_key', 'secret_key')):
        raise ValueError("The production authentication file is incomplete.")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    snapshot = Path(tempfile.mkdtemp(prefix='snapshot-', dir=destination))
    try:
        for name, payload in (('family-finances.vault', vault), ('.auth.json', auth)):
            path = snapshot / name
            with path.open('xb') as handle:
                os.chmod(path, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if path.read_bytes() != payload:
                raise OSError("The encrypted snapshot could not be verified.")
        metadata = snapshot / '.production-mirror.json'
        metadata.write_text(json.dumps({'copied_at': datetime.now(timezone.utc).isoformat()}))
        os.chmod(metadata, 0o600)
    except Exception:
        shutil.rmtree(snapshot)
        raise
    return snapshot
