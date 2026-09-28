// Speech-to-text worker for Clipper: Whisper-base (int8) on ONNX Runtime WebAssembly.
// Runs entirely in the viewer's browser. The engine and model ship next to this file,
// split into pieces the artifact host will serve (<=15 MB; model weights as base64 text)
// and reassembled here.
// Assembled files are kept in IndexedDB so later visits skip the ~100 MB download.
// fetch-assets.sh rebuilds the chunks from npm.

const BASE = new URL("./", self.location.href).href;
const FILES = {
  "ort.wasm": ["ort-wasm.0.wasm", "ort-wasm.1.wasm"],
  "onnx/encoder_model_quantized.onnx": [0, 1, 2].map(i => `model/encoder.${i}.b64.txt`),
  "onnx/decoder_model_merged_quantized.onnx": [0, 1, 2, 3, 4].map(i => `model/decoder.${i}.b64.txt`),
};
for (const f of ["config.json", "generation_config.json", "preprocessor_config.json", "tokenizer.json",
  "tokenizer_config.json", "added_tokens.json", "special_tokens_map.json", "normalizer.json", "vocab.json", "merges.txt"]) {
  FILES[f] = ["model/" + f];
}
const CACHE_VERSION = "whisper-base-q8-v1";
let asr = null, loading = null, wordTimestamps = true;

// ---- IndexedDB cache (best effort: private windows or blocked storage just re-download) ----
function idb() {
  return new Promise((res, rej) => {
    const r = indexedDB.open("clipper-asr", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("files");
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
}
async function idbGet(key) {
  try {
    const db = await idb();
    return await new Promise(res => {
      const q = db.transaction("files").objectStore("files").get(CACHE_VERSION + "/" + key);
      q.onsuccess = () => res(q.result || null); q.onerror = () => res(null);
    });
  } catch { return null; }
}
async function idbPut(key, val) {
  try {
    const db = await idb();
    db.transaction("files", "readwrite").objectStore("files").put(val, CACHE_VERSION + "/" + key);
  } catch {}
}

// ---- download + reassemble ---------------------------------------------------------
const TOTAL_BYTES = 128_500_000;  // bytes on the wire, base64 included
let doneBytes = 0;
function report(extra) { self.postMessage({ type: "progress", loaded: Math.min(doneBytes, TOTAL_BYTES), total: TOTAL_BYTES, ...extra }); }

async function fetchBytes(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`Couldn't load ${url.split("/").pop()} (${r.status})`);
  if (!r.body) { const b = new Uint8Array(await r.arrayBuffer()); doneBytes += b.length; report(); return b; }
  const reader = r.body.getReader(), parts = []; let n = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parts.push(value); n += value.length; doneBytes += value.length;
    if (parts.length % 16 === 0) report();
  }
  const out = new Uint8Array(n); let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return url.endsWith(".b64.txt") ? fromBase64(out) : out;
}
function fromBase64(ascii) {
  const str = new TextDecoder().decode(ascii).trim();
  if (Uint8Array.fromBase64) return Uint8Array.fromBase64(str);
  const bin = atob(str), out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
const pending = new Map();
function getFile(key) {
  if (!pending.has(key)) pending.set(key, (async () => {
    const hit = await idbGet(key);
    if (hit) { doneBytes += hit.byteLength; report(); return hit; }
    const chunks = await Promise.all(FILES[key].map(p => fetchBytes(BASE + p)));
    const size = chunks.reduce((a, c) => a + c.length, 0);
    const buf = new Uint8Array(size); let o = 0;
    for (const c of chunks) { buf.set(c, o); o += c.length; }
    await idbPut(key, buf.buffer);
    return buf.buffer;
  })());
  return pending.get(key);
}

// transformers.js asks this "cache" for every model file; we answer from our chunks.
const customCache = {
  async match(req) {
    const url = typeof req === "string" ? req : req.url;
    const key = Object.keys(FILES).find(k => k !== "ort.wasm" && url.endsWith("/" + k));
    if (!key) return undefined;
    const type = key.endsWith(".json") ? "application/json" : key.endsWith(".txt") ? "text/plain" : "application/octet-stream";
    return new Response(await getFile(key), { headers: { "content-type": type } });
  },
  async put() {},
};

async function load() {
  report({ stage: "engine" });
  const { pipeline, env } = await import(BASE + "transformers.min.js");
  env.allowRemoteModels = false;
  env.allowLocalModels = true;
  env.localModelPath = BASE + "virtual-models/";
  env.useBrowserCache = false;
  env.useCustomCache = true;
  env.customCache = customCache;
  const ort = env.backends.onnx;
  ort.wasm.wasmBinary = await getFile("ort.wasm");
  ort.wasm.wasmPaths = { mjs: BASE + "ort-wasm-simd-threaded.jsep.mjs" };
  ort.wasm.numThreads = 1;
  ort.wasm.proxy = false;
  report({ stage: "model" });
  // Warm every file in parallel so the progress bar reflects the real download.
  await Promise.all(Object.keys(FILES).map(getFile));
  asr = await pipeline("automatic-speech-recognition", "Xenova/whisper-base", { dtype: "q8", device: "wasm" });
  doneBytes = TOTAL_BYTES; report({ stage: "ready" });
}

// Split segment text into words spread across the segment by character length.
function wordsFromSegments(chunks, end) {
  const words = [];
  for (const c of chunks || []) {
    const s = c.timestamp?.[0] ?? 0, e = c.timestamp?.[1] ?? end;
    const toks = String(c.text || "").trim().split(/\s+/).filter(Boolean);
    const total = toks.reduce((a, t) => a + t.length + 1, 0) || 1;
    let t = s;
    for (const w of toks) {
      const d = (e - s) * (w.length + 1) / total;
      words.push({ text: w, start: t, end: t + d }); t += d;
    }
  }
  return words;
}

async function transcribe(audio) {
  const dur = audio.length / 16000;
  // Each clip window is <= 30 s, Whisper's native input, so no chunking (chunking dropped the tail).
  const opts = { language: "english", task: "transcribe" };
  if (wordTimestamps) {
    try {
      const out = await asr(audio, { ...opts, return_timestamps: "word" });
      const words = (out.chunks || []).map(c => ({ text: String(c.text).trim(), start: c.timestamp?.[0] ?? 0,
        end: c.timestamp?.[1] ?? dur })).filter(w => w.text);
      // Word alignment occasionally collapses (every word on one timestamp); segments are safer then.
      const spread = new Set(words.map(w => Math.round(w.start * 10))).size;
      if (words.length && spread >= Math.min(4, words.length) / 2) {
        return { text: String(out.text || "").trim(), words, timing: "word" };
      }
    } catch (e) {
      wordTimestamps = false;  // this browser/model can't do word timing: use segments from now on
    }
  }
  const out = await asr(audio, { ...opts, return_timestamps: true });
  return { text: String(out.text || "").trim(), words: wordsFromSegments(out.chunks, dur), timing: "segment" };
}

self.onmessage = async ({ data }) => {
  try {
    if (data.type === "load") {
      loading ??= load();
      await loading;
    } else if (data.type === "transcribe") {
      loading ??= load();
      await loading;
      const res = await transcribe(data.audio);
      self.postMessage({ type: "result", id: data.id, ...res });
    }
  } catch (e) {
    self.postMessage({ type: "error", id: data.id, message: String(e?.message || e) });
  }
};
