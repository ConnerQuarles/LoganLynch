#!/usr/bin/env bash
# Rebuilds the speech-to-text files the Clipper page loads next to asr-worker.js:
# transformers.js 3.8.1 (engine + ONNX Runtime WebAssembly) and Whisper-base int8
# (transformers.js format), from npm, integrity-checked, and split to fit the artifact
# host: it serves .wasm but not raw model files, and caps files at 15 MB (16 MB text).
# So the engine goes up as .wasm pieces and the model weights as base64 .txt pieces.
# The outputs are large and generated, so they're gitignored.
set -euo pipefail
cd "$(dirname "$0")"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

fetch() { # url integrity dest
  curl -fsSL -o "$3" "$1"
  python3 - "$3" "$2" <<'PY'
import sys, hashlib, base64
got = "sha512-" + base64.b64encode(hashlib.sha512(open(sys.argv[1], "rb").read()).digest()).decode()
sys.exit(0 if got == sys.argv[2] else f"integrity mismatch for {sys.argv[1]}")
PY
}
fetch https://registry.npmjs.org/@huggingface/transformers/-/transformers-3.8.1.tgz \
  "$(curl -fsSL https://registry.npmjs.org/@huggingface/transformers/3.8.1 | python3 -c 'import sys,json;print(json.load(sys.stdin)["dist"]["integrity"])')" "$tmp/tf.tgz"
fetch https://registry.npmjs.org/sts-whisper-base/-/sts-whisper-base-1.0.0.tgz \
  "sha512-lBXHIZX66KiR4+TMl2zCB4NO/Ih3Vp06l1yDjYtG9rROlLVPq3s7DiBtTRkeFQiDh0Zl3x+OmRuse17Fyjq20w==" "$tmp/wb.tgz"
mkdir -p "$tmp/tf" "$tmp/wb" && tar xzf "$tmp/tf.tgz" -C "$tmp/tf" && tar xzf "$tmp/wb.tgz" -C "$tmp/wb"

rm -rf model ./*.bin ./*.wasm && mkdir -p model
cp "$tmp/tf/package/dist/transformers.min.js" "$tmp/tf/package/dist/ort-wasm-simd-threaded.jsep.mjs" .
# The artifact host rejects files containing U+FFFD. transformers.js has it only inside
# three string literals ("\uFFFD" checks in Whisper word timing), where the escape is identical.
python3 - <<'PY'
s = open("transformers.min.js", encoding="utf-8").read()
assert s.count('"\ufffd"') == s.count("\ufffd"), "U+FFFD outside a string literal"
open("transformers.min.js", "w", encoding="utf-8").write(s.replace('"\ufffd"', '"\\uFFFD"'))
PY
M="$tmp/wb/package/models/Xenova/whisper-base"
cp "$M"/*.json "$M/merges.txt" model/
python3 - "$tmp/tf/package/dist/ort-wasm-simd-threaded.jsep.wasm" "$M/onnx" <<'PY'
import sys, base64
def split(src, prefix, n, b64=False):
    data = open(src, "rb").read(); size = -(-len(data) // n)
    for i in range(n):
        part = data[i * size:(i + 1) * size]
        if b64:
            open(f"{prefix}.{i}.b64.txt", "wb").write(base64.b64encode(part))
        else:
            open(f"{prefix}.{i}.wasm", "wb").write(part)
split(sys.argv[1], "ort-wasm", 2)                                                      # 2 x 10.8 MB
split(sys.argv[2] + "/encoder_model_quantized.onnx", "model/encoder", 3, b64=True)       # 3 x 10.3 MB b64
split(sys.argv[2] + "/decoder_model_merged_quantized.onnx", "model/decoder", 5, b64=True) # 5 x 14.3 MB b64
PY
echo "Whisper assets ready in $(pwd)"; du -sh .
