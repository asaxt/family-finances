"""Stdlib-only, bounded private pipe client for the isolated classifier runtime."""
import json
import math
import os
from pathlib import Path
import secrets
import selectors
import subprocess
import threading
import time

from classifier_runtime import (
    ClassifierInputTooLongError, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES,
    MAX_TEXTS, input_text, parse_json, validate_definitions, validate_scores,
)

WORKER_SCRIPT = Path(__file__).with_name("classifier_worker.py")


class ClassifierWorkerError(RuntimeError):
    def __init__(self):
        super().__init__("The local classifier could not complete this request.")


class ClassifierWorker:
    def __init__(self, model_path, device="mps", python_path=None, timeout=120):
        if device not in {"cpu", "mps"} or type(timeout) not in (int, float) or not 0 < timeout <= 600:
            raise ValueError("Invalid classifier worker configuration.")
        if not math.isfinite(timeout):
            raise ValueError("Invalid classifier worker configuration.")
        try:
            self.model_path = Path(model_path).resolve(strict=True)
            self.python_path = Path(python_path or Path(__file__).parent / ".classifier-venv/bin/python").absolute()
            if not self.model_path.is_dir() or not os.access(self.python_path, os.X_OK):
                raise ValueError()
        except (OSError, ValueError):
            raise ClassifierWorkerError() from None
        self.device, self.timeout = device, timeout
        self._process = None
        self._closed = False
        self._lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _start(self):
        if self._closed:
            raise ClassifierWorkerError()
        if self._process is None:
            env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1",
                   "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1",
                   "HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
                   "DO_NOT_TRACK": "1", "TOKENIZERS_PARALLELISM": "false"}
            self._process = subprocess.Popen(
                [str(self.python_path), "-I", "-u", str(WORKER_SCRIPT.resolve()),
                 "--model", str(self.model_path), "--device", self.device],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                shell=False, bufsize=0, env=env, close_fds=True,
            )
            os.set_blocking(self._process.stdin.fileno(), False)
            os.set_blocking(self._process.stdout.fileno(), False)

    def scores(self, rows, definitions):
        try:
            validate_definitions(definitions)
            if not isinstance(rows, list):
                raise ValueError()
            texts = [input_text(row) for row in rows]
        except (ValueError, TypeError, AttributeError):
            raise ClassifierWorkerError() from None
        results = []
        with self._lock:
            if self._closed:
                raise ClassifierWorkerError()
            offset = 0
            # Long category names consume response bytes for every text. This
            # conservative estimate keeps large palettes below the byte cap.
            row_bytes = sum(len(json.dumps(key)) + 28 for key in [*definitions, ""]) + 4
            maximum = max(1, min(MAX_TEXTS, (MAX_RESPONSE_BYTES - 128) // row_bytes))
            while offset < len(texts):
                count = min(maximum, len(texts) - offset)
                nonce = secrets.token_hex(16)
                while True:
                    packet = {"nonce": nonce, "texts": texts[offset:offset + count], "definitions": definitions}
                    raw = (json.dumps(packet, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode()
                    if len(raw) <= MAX_REQUEST_BYTES:
                        break
                    if count == 1:
                        raise ClassifierWorkerError()
                    count = max(1, count // 2)
                try:
                    self._start()
                    response = parse_json(self._exchange(raw))
                    if not isinstance(response, dict) or response.get("nonce") != nonce:
                        raise ValueError()
                    if set(response) == {"nonce", "error"} and response["error"] == "input_too_long":
                        raise ClassifierInputTooLongError()
                    if set(response) != {"nonce", "scores"}:
                        raise ValueError()
                    results.extend(validate_scores(response["scores"], definitions, count))
                except ClassifierInputTooLongError:
                    raise
                except Exception:
                    self._stop()
                    raise ClassifierWorkerError() from None
                offset += count
        return results

    def _exchange(self, raw):
        process = self._process
        if process.poll() is not None:
            raise ValueError()
        offset, received = 0, bytearray()
        deadline = time.monotonic() + self.timeout
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                events = selector.select(remaining)
                if not events:
                    raise TimeoutError()
                for key, _ in events:
                    if key.fileobj is process.stdin:
                        try:
                            offset += os.write(process.stdin.fileno(), raw[offset:offset + 8192])
                        except BlockingIOError:
                            continue
                        if offset == len(raw):
                            selector.unregister(process.stdin)
                    else:
                        try:
                            chunk = os.read(process.stdout.fileno(), min(65536, MAX_RESPONSE_BYTES + 1 - len(received)))
                        except BlockingIOError:
                            continue
                        if not chunk:
                            raise ValueError()
                        received.extend(chunk)
                        if len(received) > MAX_RESPONSE_BYTES:
                            raise ValueError()
                        if b"\n" in received:
                            line, extra = received.split(b"\n", 1)
                            if extra or offset != len(raw):
                                raise ValueError()
                            return bytes(line)

    def _stop(self):
        process, self._process = self._process, None
        self._closed = True
        if process is not None:
            try:
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                process.wait(timeout=5)
            finally:
                process.stdin.close()
                process.stdout.close()

    def close(self):
        with self._lock:
            self._stop()
