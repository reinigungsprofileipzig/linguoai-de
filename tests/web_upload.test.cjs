/* Run with: node --test tests/web_upload.test.cjs */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { webcrypto, createHash } = require("node:crypto");

class Element {
  constructor() {
    this.hidden = false;
    this.disabled = false;
    this.dataset = {};
    this.textContent = "";
    this.children = [];
    this.files = [];
    this.value = "";
    this.listeners = {};
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  setAttribute() {}
  removeAttribute() {}
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  querySelector() { return new Element(); }
  focus() { this.focused = true; }
  scrollIntoView() {}
}

function fixture(configuration = {}) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const calls = [];
  const delays = [];
  let context;
  let session = null;
  let uniqueCreates = 0;
  let putAttempts = 0;
  let finalizePolls = 0;
  let abandoned = false;
  let createLost = Boolean(configuration.createLost);
  let chunkLost = Boolean(configuration.chunkLost);
  let completeLost = Boolean(configuration.completeLost);
  let chunkFailures = configuration.chunkFailures || 0;
  const chunkBytes = configuration.chunkBytes || 8 * 1024 * 1024;

  const row = () => {
    const result = { ...session };
    if (result.status === "completed" || result.status === "aborted") result.job = {
      id: result.job_id, filename: "sample.mp4",
      status: result.status === "aborted" ? "cancelled" : "queued",
      progress: 0, artifacts: [],
    };
    return result;
  };

  function jsonResponse(status, data) {
    return { ok: status >= 200 && status < 300, status, json: async () => data };
  }

  async function fetch(url, options = {}) {
    const address = new URL(url);
    const method = options.method || "GET";
    calls.push({ method, path: address.pathname, headers: options.headers, body: options.body });
    assert.ok(address.pathname.startsWith("/linguoai/api/"), "the reverse proxy subpath is retained");
    assert.equal(options.credentials, "same-origin");
    if (method !== "GET") assert.equal(options.headers["X-LinguoAI-Request"], "1");
    if (method === "POST" && address.pathname.endsWith("/uploads")) {
      assert.equal(options.headers["Content-Type"], "application/json");
      assert.match(options.headers["X-Upload-Request-ID"], /^[A-Za-z0-9_-]{20,80}$/);
      const body = JSON.parse(options.body);
      assert.equal(body.options.asr_engine, "parakeet");
      if (!session) {
        uniqueCreates += 1;
        session = {
          id: "upload-session", job_id: "queued-job", offset: 0,
          size_bytes: body.size_bytes, chunk_bytes: chunkBytes, status: "uploading",
        };
        if (configuration.invalidOffset) session.offset = body.size_bytes + 1;
      }
      if (createLost) { createLost = false; throw new TypeError("create response lost after commit"); }
      return jsonResponse(201, row());
    }
    if (method === "POST" && address.pathname.endsWith("/auth/logout")) return jsonResponse(200, { authenticated: false });
    if (method === "DELETE") {
      abandoned = true;
      session.status = "aborted";
      return jsonResponse(200, row());
    }
    if (method === "POST" && address.pathname.endsWith("/complete")) {
      assert.equal(session.offset, session.size_bytes);
      if (session.status === "uploading") session.status = "finalizing";
      if (completeLost) { completeLost = false; return jsonResponse(503, { detail: "complete response temporarily lost" }); }
      return jsonResponse(202, row());
    }
    if (method === "GET" && address.pathname.endsWith("/uploads/upload-session")) {
      if (session.status === "finalizing" && ++finalizePolls >= 2) session.status = "completed";
      return jsonResponse(200, row());
    }
    throw new Error("Unexpected fetch " + method + " " + address.pathname);
  }

  class Xhr {
    constructor() {
      this.headers = {};
      this.listeners = {};
      this.upload = { addEventListener: (name, callback) => { this.progress = callback; } };
      this.aborted = false;
      this.status = 0;
    }
    open(method, url) { this.method = method; this.url = new URL(url); }
    setRequestHeader(name, value) { this.headers[name] = value; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name) { this.listeners[name]?.({}); }
    abort() {
      if (this.aborted) return;
      this.aborted = true;
      this.emit("abort");
    }
    send(chunk) {
      putAttempts += 1;
      calls.push({ method: this.method, path: this.url.pathname, headers: this.headers, bytes: chunk.size, offset: Number(this.url.searchParams.get("offset")) });
      assert.equal(this.method, "PUT");
      assert.equal(this.url.pathname, "/linguoai/api/uploads/upload-session");
      assert.equal(this.headers["X-LinguoAI-Request"], "1");
      assert.equal(this.headers["Content-Type"], "application/octet-stream");
      assert.equal(this.withCredentials, true);
      assert.ok(chunk.size > 0 && chunk.size <= 8 * 1024 * 1024);
      void (async () => {
        const bytes = Buffer.from(await chunk.arrayBuffer());
        assert.equal(this.headers["X-Chunk-SHA256"], createHash("sha256").update(bytes).digest("hex"));
        if (configuration.holdChunk) { configuration.onHold?.(); return; }
        if (configuration.cancelChunk) {
          await context.__client.state.upload.abort();
          return;
        }
        if (configuration.chunkStatus) {
          this.status = configuration.chunkStatus;
          this.responseText = JSON.stringify({ detail: "permanent upload rejection" });
          this.emit("load");
          return;
        }
        if (chunkFailures > 0 || configuration.alwaysFailChunk) {
          chunkFailures -= 1;
          this.emit("error");
          return;
        }
        if (this.aborted || abandoned) return;
        assert.equal(Number(this.url.searchParams.get("offset")), session.offset, "chunks remain contiguous after recovery");
        this.progress?.({ lengthComputable: true, loaded: Math.floor(chunk.size / 2), total: chunk.size });
        this.progress?.({ lengthComputable: true, loaded: chunk.size, total: chunk.size });
        session.offset += chunk.size;
        if (chunkLost) { chunkLost = false; this.emit("error"); return; }
        this.status = 200;
        this.responseText = JSON.stringify(row());
        this.emit("load");
      })().catch((error) => {
        configuration.internalErrors?.push(error);
        this.responseText = JSON.stringify({ detail: error.message });
        this.status = 500;
        this.emit("load");
      });
    }
  }

  const window = { location: { href: "https://example.test/linguoai/web.html", origin: "https://example.test" }, addEventListener() {} };
  context = vm.createContext({
    console, URL, Uint8Array, AbortController, Blob, crypto: webcrypto, XMLHttpRequest: Xhr,
    fetch, window,
    document: { getElementById: element, createElement: () => new Element(), body: new Element() },
    setTimeout: (callback, milliseconds) => {
      if (milliseconds < 10000) delays.push(milliseconds);
      if ((configuration.cancelBackoff && milliseconds === 1000) || (configuration.cancelFinalize && milliseconds === 1500)) {
        setTimeout(() => context.__client.state.upload.abort(), 0);
      }
      return setTimeout(callback, milliseconds < 10000 ? 1 : milliseconds);
    },
    clearTimeout,
  });
  let source = fs.readFileSync("website/assets/web-app.js", "utf8").replace(/\r\n/g, "\n");
  const oldTail = "  connect();\n})();";
  assert.ok(source.endsWith(oldTail + "\n") || source.endsWith(oldTail));
  source = source.replace(oldTail, "  globalThis.__client = { uploadFile, state };\n})();");
  vm.runInContext(source, context, { filename: "web-app.js" });
  context.__client.state.authenticated = true;
  context.__client.state.uploading = true;
  const file = new Blob([Buffer.alloc(configuration.size || 2 * chunkBytes + 137, 17)]);
  file.name = "sample.mp4";
  return {
    client: context.__client, file, calls, delays, elements,
    uniqueCreates: () => uniqueCreates, putAttempts: () => putAttempts,
    session: () => session,
    start: () => context.__client.uploadFile(file, { target_language: "en", source_language: "de", asr_engine: "parakeet" }),
  };
}

test("uploads a file larger than two chunks with real SHA-256, relative routes and finalization polling", async () => {
  const errors = [];
  const f = fixture({ internalErrors: errors });
  const job = await f.start();
  assert.equal(job.id, "queued-job");
  assert.equal(job.status, "queued");
  assert.equal(f.putAttempts(), 3);
  assert.equal(f.session().offset, f.file.size);
  assert.equal(f.elements.get("job-progress").value, 100);
  assert.equal(f.uniqueCreates(), 1);
  assert.deepEqual(errors, []);
  assert.equal(f.calls.filter((call) => call.method === "POST" && call.path.endsWith("/complete")).length, 1);
});

test("a lost create response reuses the UUID and creates only one staging job", async () => {
  const f = fixture({ createLost: true, chunkBytes: 4096 });
  await f.start();
  const creates = f.calls.filter((call) => call.method === "POST" && call.path.endsWith("/uploads"));
  assert.equal(creates.length, 2);
  assert.equal(new Set(creates.map((call) => call.headers["X-Upload-Request-ID"])).size, 1);
  assert.equal(f.uniqueCreates(), 1);
});

test("a lost chunk acknowledgement reads the committed offset instead of duplicating data", async () => {
  const f = fixture({ chunkLost: true, chunkBytes: 4096 });
  await f.start();
  assert.equal(f.putAttempts(), 3);
  const puts = f.calls.filter((call) => call.method === "PUT");
  assert.deepEqual(puts.map((call) => call.offset), [0, 4096, 8192]);
});

test("transient failed chunks retry with bounded backoff and identical offset/hash", async () => {
  const f = fixture({ chunkFailures: 2, chunkBytes: 4096, size: 100 });
  await f.start();
  assert.equal(f.putAttempts(), 3);
  const puts = f.calls.filter((call) => call.method === "PUT");
  assert.deepEqual(puts.map((call) => call.offset), [0, 0, 0]);
  assert.equal(new Set(puts.map((call) => call.headers["X-Chunk-SHA256"])).size, 1);
  assert.ok(f.delays.includes(1000) && f.delays.includes(2000));
});

test("a lost completion response polls the same finalizer without sending file data twice", async () => {
  const f = fixture({ completeLost: true, chunkBytes: 4096, size: 100 });
  await f.start();
  assert.equal(f.putAttempts(), 1);
  assert.equal(f.calls.filter((call) => call.path.endsWith("/complete")).length, 2);
  assert.equal(f.uniqueCreates(), 1);
});

test("permanent 403 rejects directly and deletes staging without chunk retries", async () => {
  const f = fixture({ chunkStatus: 403, chunkBytes: 4096, size: 100 });
  await assert.rejects(f.start(), (error) => error.status === 403);
  assert.equal(f.putAttempts(), 1);
  assert.equal(f.calls.filter((call) => call.method === "DELETE").length, 1);
});

test("a permanently unavailable connection stops after three retries and deletes staging", async () => {
  const f = fixture({ alwaysFailChunk: true, chunkBytes: 4096, size: 100 });
  await assert.rejects(f.start(), (error) => error.status === 0);
  assert.equal(f.putAttempts(), 4);
  assert.deepEqual(f.delays.filter((delay) => delay >= 1000), [1000, 2000, 4000]);
  assert.equal(f.calls.filter((call) => call.method === "DELETE").length, 1);
});

test("cancelling in-flight transfer aborts it and returns the real cancelled backend job", async () => {
  const f = fixture({ cancelChunk: true, chunkBytes: 4096 });
  await assert.rejects(f.start(), (error) => error.status === -1 && error.job?.status === "cancelled");
  assert.equal(f.putAttempts(), 1);
  assert.equal(f.calls.filter((call) => call.method === "DELETE").length, 1);
  assert.equal(f.session().offset, 0);
  assert.equal(f.session().status, "aborted");
});

test("invalid offsets and an unsafe chunk size are rejected before sending file bytes", async () => {
  for (const configuration of [{ invalidOffset: true }, { chunkBytes: 9 * 1024 * 1024 }]) {
    const f = fixture({ ...configuration, size: 100 });
    await assert.rejects(f.start(), (error) => error.status === 422);
    assert.equal(f.putAttempts(), 0);
  }
});

test("cancellation interrupts retry backoff before another file chunk is sent", async () => {
  const f = fixture({ chunkFailures: 1, cancelBackoff: true, chunkBytes: 4096, size: 100 });
  await assert.rejects(f.start(), (error) => error.status === -1);
  assert.equal(f.putAttempts(), 1);
  assert.equal(f.calls.filter((call) => call.method === "DELETE").length, 1);
});

test("cancellation during finalization deletes staging and returns the cancelled job", async () => {
  const f = fixture({ cancelFinalize: true, chunkBytes: 4096, size: 100 });
  await assert.rejects(f.start(), (error) => error.status === -1 && error.job?.status === "cancelled");
  assert.equal(f.putAttempts(), 1);
  assert.equal(f.calls.filter((call) => call.method === "DELETE").length, 1);
  assert.equal(f.session().status, "aborted");
});

test("signing out cancels staging before clearing the authenticated session", async () => {
  let held;
  const ready = new Promise((resolve) => { held = resolve; });
  const f = fixture({ holdChunk: true, onHold: held, chunkBytes: 4096, size: 100 });
  const result = f.start().then(() => null, (error) => error);
  await ready;
  await f.elements.get("logout").listeners.click[0]();
  const error = await result;
  assert.equal(error.status, -1);
  assert.equal(f.client.state.authenticated, false);
  const deleteIndex = f.calls.findIndex((call) => call.method === "DELETE");
  const logoutIndex = f.calls.findIndex((call) => call.path.endsWith("/auth/logout"));
  assert.ok(deleteIndex >= 0 && logoutIndex > deleteIndex);
  assert.equal(f.calls.filter((call) => call.method === "DELETE").length, 1);
});
