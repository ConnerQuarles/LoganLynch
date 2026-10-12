"""Build the speech-to-text files the Clipper page loads next to whisper/asr-worker.js.

    python -m clipper.assets        # (the local website runs this on first start)

Downloads transformers.js 3.8.1 (engine + ONNX Runtime WebAssembly) and Whisper-base
int8 (transformers.js format) from npm, checks their integrity hashes, and splits them
to fit the claude.ai artifact host, which serves .wasm but not raw model files and caps
files at 15 MB (16 MB for text): the engine goes out as .wasm pieces and the model
weights as base64 .txt pieces. The local website serves the very same files, so one
worker works in both places. Outputs are generated and gitignored.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import ssl
import sys
import tarfile
import urllib.request
from pathlib import Path

OUT = Path(__file__).parent / "browser" / "whisper"
MARKER = ".complete"
VERSION = "transformers-3.8.1 whisper-base-q8 v1"
TRANSFORMERS = "https://registry.npmjs.org/@huggingface/transformers/-/transformers-3.8.1.tgz"
TRANSFORMERS_META = "https://registry.npmjs.org/@huggingface/transformers/3.8.1"
WHISPER = "https://registry.npmjs.org/sts-whisper-base/-/sts-whisper-base-1.0.0.tgz"
WHISPER_SHA512 = "sha512-lBXHIZX66KiR4+TMl2zCB4NO/Ih3Vp06l1yDjYtG9rROlLVPq3s7DiBtTRkeFQiDh0Zl3x+OmRuse17Fyjq20w=="
MODEL_DIR = "package/models/Xenova/whisper-base/"
MODEL_TEXT = ["config.json", "generation_config.json", "preprocessor_config.json", "tokenizer.json",
              "tokenizer_config.json", "added_tokens.json", "special_tokens_map.json", "normalizer.json",
              "vocab.json", "merges.txt"]


def ready(out: Path = OUT) -> bool:
    marker = out / MARKER
    return marker.is_file() and marker.read_text().strip() == VERSION


def _ssl_context() -> ssl.SSLContext:
    # python.org builds on macOS often ship without root certificates; certifi fixes that.
    bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if bundle:
        return ssl.create_default_context(cafile=bundle)
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs():
        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
    return ctx


def _get(url: str, label: str, log) -> bytes:
    with urllib.request.urlopen(url, timeout=120, context=_ssl_context()) as r:
        total = int(r.headers.get("Content-Length") or 0)
        parts, got, shown = [], 0, -1
        while chunk := r.read(1 << 20):
            parts.append(chunk)
            got += len(chunk)
            pct = int(got * 100 / total) if total else -1
            if pct >= shown + 10:
                shown = pct
                log(f"  {label}: {pct}%" if pct >= 0 else f"  {label}: {got >> 20} MB")
    return b"".join(parts)


def _check(data: bytes, integrity: str, what: str) -> None:
    got = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if got != integrity:
        raise RuntimeError(f"{what} failed its integrity check; not using it.")


def _member(tar: tarfile.TarFile, name: str) -> bytes:
    f = tar.extractfile(name)   # read by exact name only: nothing is written from the archive's paths
    if f is None:
        raise RuntimeError(f"{name} missing from package")
    return f.read()


def _split(data: bytes, prefix: Path, n: int, b64: bool) -> None:
    size = -(-len(data) // n)
    for i in range(n):
        part = data[i * size:(i + 1) * size]
        if b64:
            (prefix.parent / f"{prefix.name}.{i}.b64.txt").write_bytes(base64.b64encode(part))
        else:
            (prefix.parent / f"{prefix.name}.{i}.wasm").write_bytes(part)


def build(out: Path = OUT, log=print) -> None:
    log("Setting up speech-to-text (first run only, about 130 MB)…")
    meta = json.loads(_get(TRANSFORMERS_META, "engine info", lambda *_: None))
    tf = _get(TRANSFORMERS, "engine", log)
    _check(tf, meta["dist"]["integrity"], "transformers.js")
    wb = _get(WHISPER, "speech model", log)
    _check(wb, WHISPER_SHA512, "Whisper model")

    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).unlink(missing_ok=True)
    model = out / "model"
    model.mkdir(exist_ok=True)
    for old in [*out.glob("ort-wasm.*.wasm"), *model.glob("*")]:
        old.unlink()

    with tarfile.open(fileobj=io.BytesIO(tf), mode="r:gz") as tar:
        js = _member(tar, "package/dist/transformers.min.js").decode("utf-8")
        # The artifact host rejects files containing U+FFFD; transformers.js has it only in three
        # string literals (Whisper word-timing checks), where the � escape is identical.
        if js.count('"�"') != js.count("�"):
            raise RuntimeError("unexpected U+FFFD outside a string literal in transformers.min.js")
        (out / "transformers.min.js").write_text(js.replace('"�"', '"\\uFFFD"'), encoding="utf-8")
        (out / "ort-wasm-simd-threaded.jsep.mjs").write_bytes(_member(tar, "package/dist/ort-wasm-simd-threaded.jsep.mjs"))
        _split(_member(tar, "package/dist/ort-wasm-simd-threaded.jsep.wasm"), out / "ort-wasm", 2, b64=False)
    with tarfile.open(fileobj=io.BytesIO(wb), mode="r:gz") as tar:
        for name in MODEL_TEXT:
            (model / name).write_bytes(_member(tar, MODEL_DIR + name))
        _split(_member(tar, MODEL_DIR + "onnx/encoder_model_quantized.onnx"), model / "encoder", 3, b64=True)
        _split(_member(tar, MODEL_DIR + "onnx/decoder_model_merged_quantized.onnx"), model / "decoder", 5, b64=True)
    (out / MARKER).write_text(VERSION)
    log("Speech-to-text ready.")


def ensure(out: Path = OUT, log=print) -> bool:
    """Build the files if they're missing. Returns False (and explains) if that failed."""
    if ready(out):
        return True
    try:
        build(out, log)
        return True
    except Exception as exc:  # no internet, proxy, disk full…
        log(f"Couldn't set up speech-to-text ({exc}). Clipper still works, just without captions;"
            " restart it to try again.")
        return False


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    sys.exit(0 if ensure(target) else 1)
