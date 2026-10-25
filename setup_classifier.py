"""Explicitly download the pinned classifier model; never opens application data."""
import json
import os
from pathlib import Path

from classifier_runtime import MODEL_REPO, MODEL_REVISION


def download_model(cache_dir=None):
    for key in ('HF_HUB_DISABLE_TELEMETRY', 'HF_HUB_DISABLE_IMPLICIT_TOKEN', 'DO_NOT_TRACK'):
        os.environ[key] = '1'
    from huggingface_hub import snapshot_download

    root = Path(cache_dir) if cache_dir is not None else Path(__file__).parent / '.classifier-cache'
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if not isinstance(manifest, dict):
        raise ValueError('Invalid local model manifest.')
    path = snapshot_download(
        repo_id=MODEL_REPO, revision=MODEL_REVISION, cache_dir=str(root / 'hub'), token=False,
        allow_patterns=['*.json', '*.safetensors', '*.txt', '*.model'],
    )
    manifest[MODEL_REPO] = {'revision': MODEL_REVISION, 'path': str(Path(path).resolve())}
    temporary = root / 'manifest.json.tmp'
    temporary.write_text(json.dumps(manifest, indent=2) + '\n')
    temporary.replace(manifest_path)


if __name__ == '__main__':
    download_model()
    print('Classifier model is cached. Categorization can now run offline.')
