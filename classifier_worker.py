"""Private JSON-lines classifier child. No application or financial-store access."""
import argparse
import contextlib
import json
from pathlib import Path
import re
import sys

# -I excludes the script directory. Add this fixed source directory, never a
# caller-provided import path, for the pure runtime module only.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from classifier_runtime import (
    ClassifierInputTooLongError, GLiClassClassifier, MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES, MAX_TEXTS, parse_json, validate_definitions, validate_scores,
)


def serve(source, destination, model_path, device, classifier_factory=GLiClassClassifier):
    classifier = None
    while True:
        raw = source.readline(MAX_REQUEST_BYTES + 1)
        if not raw:
            return 0
        nonce = ""
        try:
            if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise ValueError()
            packet = parse_json(raw)
            if not isinstance(packet, dict) or set(packet) != {"nonce", "texts", "definitions"}:
                raise ValueError()
            nonce = packet["nonce"]
            if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
                nonce = ""
                raise ValueError()
            texts, definitions = packet["texts"], packet["definitions"]
            if not isinstance(texts, list) or not 1 <= len(texts) <= MAX_TEXTS:
                raise ValueError()
            if any(not isinstance(text, str) for text in texts):
                raise ValueError()
            validate_definitions(definitions)
        except Exception:
            _write(destination, {"nonce": nonce, "error": "invalid_request"})
            return 1
        try:
            # Library diagnostics never share the protocol stream. The parent
            # discards stderr; transaction text is never logged here.
            with contextlib.redirect_stdout(sys.stderr):
                if classifier is None:
                    classifier = classifier_factory(model_path, definitions, device)
                else:
                    classifier.set_definitions(definitions)
                scores = classifier.scores_texts(texts)
            validate_scores(scores, definitions, len(texts))
            packet = {"nonce": nonce, "scores": scores}
        except ClassifierInputTooLongError:
            packet = {"nonce": nonce, "error": "input_too_long"}
        except Exception:
            _write(destination, {"nonce": nonce, "error": "unavailable"})
            return 1
        if not _write(destination, packet):
            return 1


def _write(destination, packet):
    raw = (json.dumps(packet, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode()
    if len(raw) > MAX_RESPONSE_BYTES:
        return False
    destination.write(raw)
    destination.flush()
    return True


def main():
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", choices=("cpu", "mps"), default="mps")
    args = parser.parse_args()
    return serve(sys.stdin.buffer, sys.stdout.buffer, args.model, args.device)


if __name__ == "__main__":
    raise SystemExit(main())
