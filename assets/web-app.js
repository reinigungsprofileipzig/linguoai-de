/* Authenticated, same-origin upload client. Credentials stay in the session cookie. */
(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const apiBase = new URL("api/", window.location.href);
  const mutationHeaders = { "X-LinguoAI-Request": "1" };
  const terminalStates = new Set(["completed", "failed", "cancelled", "expired"]);
  const parakeetLanguages = "bg hr cs da nl en et fi fr de el hu it lv lt mt pl pt ro sk sl es sv ru uk".split(" ");
  const statusLabels = {
    uploading: "Upload", queued: "Warteschlange", running: "In Bearbeitung",
    completed: "Fertig", failed: "Fehlgeschlagen", cancelled: "Abgebrochen", expired: "Abgelaufen",
  };
  const terminalStageLabels = {
    failed: "Verarbeitung beendet", cancelled: "Auftrag abgebrochen",
    expired: "Downloadfrist beendet", completed: "Bereit zum Download",
  };
  const stageLabels = {
    queued: "Wartet auf die Verarbeitung", starting: "Video wird vorbereitet",
    preparing: "Video wird vorbereitet", downloading: "Video wird bereitgestellt",
    extracting: "Tonspur wird vorbereitet", transcribing: "Sprache wird erkannt",
    optimizing: "Text wird aufbereitet", translating: "Text wird übersetzt",
    synthesizing: "Neue Sprachspur wird erstellt", synchronizing: "Sprachspur wird angepasst",
    muxing: "Video wird fertiggestellt", uploading: "Ergebnisse werden gespeichert",
    completed: "Bereit zum Download", failed: "Verarbeitung fehlgeschlagen",
    cancelled: "Verarbeitung abgebrochen", expired: "Downloadfrist abgelaufen",
  };
  const state = {
    capabilities: null, authenticated: false, generation: 0,
    activeJob: null, jobRevision: 0, pollTimer: null, pollController: null, expiryTimer: null,
    upload: null, uploading: false, filename: "", recentJobs: [],
    lastCapabilitiesAt: 0, connecting: false, selectionCleared: false,
  };

  class ApiError extends Error {
    constructor(message, status = 0) { super(message); this.status = status; }
  }

  function showError(id, message = "") {
    const element = byId(id);
    element.textContent = message;
    element.hidden = !message;
  }

  function notice(message, kind = "", retry = false) {
    byId("connection-message").textContent = message;
    byId("connection-notice").dataset.kind = kind;
    byId("reconnect").hidden = !retry;
  }

  function errorDetail(data, fallback) {
    if (typeof data?.detail === "string") return data.detail;
    if (typeof data?.error === "string") return data.error;
    if (Array.isArray(data?.detail)) {
      return data.detail.map((item) => item.msg || "Ungültige Eingabe").join(". ");
    }
    return fallback;
  }

  async function readResponse(response) {
    let data;
    try { data = await response.json(); }
    catch (_error) {
      throw new ApiError("Der Webdienst ist hier nicht erreichbar. Prüfe die Verbindung und versuche es erneut.", response.status);
    }
    if (!response.ok) throw new ApiError(errorDetail(data, "Die Anfrage konnte nicht abgeschlossen werden."), response.status);
    return data;
  }

  async function api(path, options = {}) {
    const generation = state.generation;
    try {
      return await readResponse(await fetch(new URL(path, apiBase), {
        ...options, credentials: "same-origin", cache: "no-store",
        headers: { ...(options.method && options.method !== "GET" ? mutationHeaders : {}), ...options.headers },
      }));
    } catch (error) {
      if (error.name === "AbortError") throw error;
      if (error.status === 401 && state.authenticated && generation === state.generation) expireSession();
      if (error instanceof ApiError) throw error;
      throw new ApiError("Die Verbindung wurde unterbrochen. Bitte versuche es erneut.");
    }
  }

  function stopPolling() {
    clearTimeout(state.pollTimer);
    clearTimeout(state.expiryTimer);
    state.pollTimer = null;
    state.expiryTimer = null;
    if (state.pollController) state.pollController.abort();
    state.pollController = null;
    state.jobRevision += 1;
  }

  function setAuthenticated(authenticated) {
    if (state.authenticated !== authenticated) state.generation += 1;
    state.authenticated = authenticated;
    byId("login-panel").hidden = authenticated;
    byId("app-panel").hidden = !authenticated;
    byId("logout").hidden = !authenticated;
    byId("workflow-steps").hidden = !authenticated;
    document.body.dataset.authenticated = String(authenticated);
    if (!authenticated) {
      stopPolling();
      if (state.upload) state.upload.abort();
      state.upload = null;
      state.activeJob = null;
      state.selectionCleared = false;
      state.recentJobs = [];
      byId("job-panel").hidden = true;
      byId("result-preview").hidden = false;
      byId("recent-panel").hidden = true;
      byId("downloads").replaceChildren();
      byId("password").value = "";
      setUploading(false);
    }
  }

  function expireSession() {
    setAuthenticated(false);
    notice("Deine Anmeldung ist abgelaufen. Melde dich erneut an, um deine Aufträge zu öffnen.", "warning");
    byId("username").focus();
  }

  function selectOptions(select, entries, preferred) {
    select.replaceChildren();
    entries.forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.append(option);
    });
    if (entries.some(([value]) => value === preferred)) select.value = preferred;
  }

  function profilesForEngine() {
    const engine = state.capabilities?.engines?.find((entry) => entry.id === "parakeet");
    const raw = engine?.profiles || [
      { id: "multilingual-v3", label: "Mehrsprachig · 25 Sprachen", languages: parakeetLanguages },
      { id: "german-primeline", label: "Deutsch · primeline", languages: ["de"] },
    ];
    return raw.map((profile) => typeof profile === "string" ? {
      id: profile,
      label: profile === "german-primeline" ? "Deutsch · primeline" : "Mehrsprachig · 25 Sprachen",
      languages: profile === "german-primeline" ? ["de"] : parakeetLanguages,
    } : profile).filter((profile) => profile.available !== false);
  }

  function updateSourceLanguages() {
    const previous = byId("source-language").value;
    const isParakeet = byId("asr-engine").value === "parakeet";
    const languages = state.capabilities?.languages || {};
    const profile = profilesForEngine().find((item) => item.id === byId("parakeet-profile").value);
    const supported = isParakeet ? (profile?.languages || (byId("parakeet-profile").value === "german-primeline" ? ["de"] : parakeetLanguages)) : Object.keys(languages);
    const entries = supported.filter((code) => languages[code]).map((code) => [code, languages[code]])
      .sort((a, b) => a[1].localeCompare(b[1], "de"));
    if (!isParakeet) entries.unshift(["", "Automatisch erkennen"]);
    selectOptions(byId("source-language"), entries, previous || (isParakeet ? "de" : ""));
    byId("source-language").required = isParakeet;
    byId("profile-field").hidden = !isParakeet;
    byId("source-hint").textContent = isParakeet
      ? "Wähle die Sprache, die im Video gesprochen wird."
      : "Whisper kann die Originalsprache automatisch erkennen.";
    byId("engine-hint").textContent = isParakeet
      ? "Parakeet unterstützt die Sprachen des gewählten Modells. Wähle die Originalsprache."
      : "Whisper bietet eine größere Sprachauswahl und automatische Spracherkennung.";
    byId("engine-badge").textContent = isParakeet ? "Parakeet" : "Whisper";
  }

  function updateServiceNote() {
    const translation = byId("translation-service").value === "nllb"
      ? "NLLB übersetzt deinen Text direkt im Dienst." : "Google Translate erhält den erkannten Text.";
    const voice = byId("tts-engine").value === "gtts"
      ? "Google TTS erhält den übersetzten Text für die Sprachausgabe."
      : "Microsoft Edge erhält den übersetzten Text für die Sprachausgabe.";
    byId("service-note").textContent = `${translation} ${voice}`;
    byId("tts-voice").disabled = byId("tts-engine").value !== "edge";
    byId("voice-hint").textContent = byId("tts-engine").value === "edge"
      ? "Bei Edge kannst du eine passende Stimme angeben, z. B. en-US-AriaNeural."
      : "Google TTS wählt die Stimme automatisch zur Zielsprache.";
  }

  function applyCapabilities(capabilities, initial = false) {
    state.capabilities = capabilities;
    state.lastCapabilitiesAt = Date.now();
    const wasAuthenticated = state.authenticated;
    setAuthenticated(Boolean(capabilities.authenticated));
    if (!state.authenticated) {
      notice(wasAuthenticated ? "Deine Anmeldung ist abgelaufen. Melde dich erneut an, um deine Aufträge zu öffnen." : "Melde dich an, um ein Video hochzuladen.", wasAuthenticated ? "warning" : "");
      return;
    }
    const engines = (capabilities.engines || []).filter((engine) => engine.available !== false);
    const previousEngine = byId("asr-engine").value;
    selectOptions(byId("asr-engine"), engines.map((engine) => [engine.id,
      engine.id === "parakeet" ? "Parakeet" : engine.id === "whisper" ? "Whisper · automatische Erkennung" : engine.label || engine.id]),
      initial || !wasAuthenticated ? capabilities.default_engine : previousEngine);
    if ((initial || !wasAuthenticated) && byId("asr-engine").value === "whisper") byId("source-language").value = "";
    const profiles = profilesForEngine();
    selectOptions(byId("parakeet-profile"), profiles.map((profile) => [profile.id, profile.label || profile.id]), byId("parakeet-profile").value || "multilingual-v3");
    const target = byId("target-language").value || "en";
    selectOptions(byId("target-language"), Object.entries(capabilities.languages || {}).sort((a, b) => a[1].localeCompare(b[1], "de")), target);
    const translation = byId("translation-service").value;
    selectOptions(byId("translation-service"), (capabilities.translation_services || [
      { id: "google", label: "Google Translate · online" },
    ]).map((service) => [service.id, service.id === "nllb" ? "NLLB · lokale Übersetzung" : service.label || service.id]), translation);
    const voice = byId("tts-engine").value;
    selectOptions(byId("tts-engine"), (capabilities.tts_engines || [
      { id: "edge", label: "Microsoft Edge · online" },
    ]).map((engine) => [engine.id, engine.label || engine.id]), voice);
    updateSourceLanguages();
    updateServiceNote();
    byId("upload-submit").disabled = state.uploading || engines.length === 0;
    byId("file-hint").textContent = `MP4, MKV, MOV, AVI, WebM oder M4V${capabilities.max_upload_bytes ? ` · maximal ${formatBytes(capabilities.max_upload_bytes)}` : ""}`;
    const retention = Number(capabilities.retention_hours);
    byId("storage-note").textContent = `Geschützte Aufbewahrung ist im Dienst enthalten.${retention > 0 ? ` Ergebnisse bleiben ${retention.toLocaleString("de")} Stunden nach Fertigstellung verfügbar.` : ""}`;
    if (!engines.length) notice("Zurzeit steht keine Spracherkennung bereit. Bitte versuche es später erneut.", "warning", true);
    else if (!capabilities.worker_online) notice("Die Verarbeitung pausiert gerade. Neue Aufträge werden gespeichert und warten, bis sie wieder verfügbar ist.", "warning");
    else notice("Bereit. Wähle dein Video und die Zielsprache.");
  }

  async function connect() {
    if (state.connecting) return;
    state.connecting = true;
    byId("reconnect").disabled = true;
    try {
      applyCapabilities(await api("capabilities"), !state.capabilities);
      if (state.authenticated) await loadRecent(true);
    } catch (error) {
      notice(error.message, "error", true);
    } finally {
      state.connecting = false;
      byId("reconnect").disabled = false;
    }
  }

  function formatBytes(bytes) {
    const number = Number(bytes) || 0;
    if (number >= 1024 ** 3) return `${(number / 1024 ** 3).toLocaleString("de", { maximumFractionDigits: 1 })} GB`;
    if (number >= 1024 ** 2) return `${(number / 1024 ** 2).toLocaleString("de", { maximumFractionDigits: 1 })} MB`;
    return `${Math.ceil(number / 1024).toLocaleString("de")} KB`;
  }

  function setStep(id) {
    const steps = ["step-file", "step-language", "step-download"];
    const current = steps.indexOf(id);
    steps.forEach((step, index) => {
      byId(step).dataset.state = index < current ? "done" : index === current ? "current" : "pending";
      if (step === id) byId(step).setAttribute("aria-current", "step");
      else byId(step).removeAttribute("aria-current");
    });
  }

  function setProgress(value) {
    const progress = Math.min(100, Math.max(0, Number(value) || 0));
    byId("job-progress").value = progress;
    byId("job-progress").textContent = `${Math.round(progress)} %`;
    byId("job-percent").textContent = `${Math.round(progress)} %`;
  }

  function filenameFor(job) {
    return job.filename || job.file_name || job.input_name || "Videoauftrag";
  }


  function timestamp(value) {
    if (value === null || value === undefined || value === "") return null;
    const numeric = typeof value === "number" || (typeof value === "string" && /^\d+(\.\d+)?$/.test(value));
    const date = new Date(numeric ? Number(value) * 1000 : value);
    return Number.isNaN(date.valueOf()) ? null : date;
  }

  function setTime(id, value) {
    const date = timestamp(value);
    const element = byId(id);
    element.textContent = date ? date.toLocaleString("de", {
      day: "2-digit", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit", timeZoneName: "short",
    }) : "";
    if (date) element.dateTime = date.toISOString();
    else element.removeAttribute("datetime");
    return date;
  }

  function renderJobTiming(job) {
    clearTimeout(state.expiryTimer);
    state.expiryTimer = null;
    const showTiming = ["completed", "expired"].includes(job.status);
    const ready = showTiming ? setTime("ready-time", job.available_at || job.finished_at) : null;
    const expiry = showTiming ? setTime("expiry-time", job.expires_at) : null;
    byId("ready-time-row").hidden = !ready;
    byId("expiry-time-row").hidden = !expiry;
    byId("expiry-time-row").querySelector("dt").textContent = job.status === "expired" ? "Downloadfrist endete" : "Download verfügbar bis";
    byId("job-timing").hidden = !ready && !expiry;
    if (job.status === "completed" && expiry && expiry.valueOf() > Date.now()) {
      const id = job.id;
      const revision = state.jobRevision;
      state.expiryTimer = setTimeout(() => {
        if (state.authenticated && state.activeJob?.id === id && state.jobRevision === revision) pollJob(id, revision);
      }, Math.min(expiry.valueOf() - Date.now() + 2000, 2147483647));
    }
  }

  function renderJob(job) {
    state.activeJob = job;
    byId("job-panel").hidden = false;
    byId("job-panel").dataset.status = job.status;
    byId("result-preview").hidden = true;
    renderJobTiming(job);
    byId("job-status").textContent = statusLabels[job.status] || "Auftrag";
    byId("job-filename").textContent = filenameFor(job);
    const headings = {
      uploading: "Datei wird gespeichert", queued: "Dein Video wartet", running: "Dein Video wird übersetzt",
      completed: "Deine neue Sprachfassung ist fertig", failed: "Auftrag konnte nicht abgeschlossen werden",
      cancelled: "Auftrag abgebrochen", expired: "Die Aufbewahrungsfrist ist abgelaufen",
    };
    byId("job-heading").textContent = headings[job.status] || "Dein Auftrag";
    byId("job-stage").textContent = terminalStageLabels[job.status] || (job.cancel_requested
      ? "Abbruch wird vorbereitet"
      : stageLabels[job.stage] || (job.status === "running" ? "Dein Video wird verarbeitet" : statusLabels[job.status] || "Status wird geladen"));
    setProgress(job.status === "completed" ? 100 : job.progress);
    const cancellable = ["uploading", "queued", "running"].includes(job.status);
    byId("cancel-job").hidden = !cancellable;
    byId("cancel-job").disabled = Boolean(job.cancel_requested);
    byId("cancel-job").textContent = job.cancel_requested ? "Abbruch angefordert" : "Abbrechen";
    byId("refresh-job").hidden = true;
    byId("prepare-job").hidden = !terminalStates.has(job.status);
    let note = "";
    if (job.cancel_requested && cancellable) note = "Der Auftrag beendet die aktuelle Arbeit und wird dann abgebrochen.";
    else if (job.status === "queued") note = state.capabilities?.worker_online
      ? "Dein Auftrag ist gespeichert und startet, sobald er an der Reihe ist. Du kannst diese Seite später wieder öffnen."
      : "Dein Auftrag ist gespeichert. Er startet, sobald die Verarbeitung wieder verfügbar ist.";
    else if (job.status === "running") note = "Du kannst diese Seite später wieder öffnen. Der aktuelle Stand bleibt in deinen Aufträgen sichtbar.";
    else if (job.status === "completed") note = "Alle Ergebnisse sind bereit. Lade sie vor Ablauf der Downloadfrist herunter.";
    else if (job.status === "expired") note = "Die Downloadfrist ist abgelaufen. Lade dein Video erneut hoch, um eine neue Sprachfassung zu erstellen.";
    byId("job-note").textContent = note;
    showError("job-error", job.status === "failed" ? (job.error || "Die Verarbeitung ist fehlgeschlagen. Versuche es erneut oder wähle eine andere Spracherkennung.") : "");
    byId("downloads").replaceChildren();
    if (job.status === "completed") {
      (job.artifacts || []).forEach((artifact) => {
        const url = new URL(artifact.download_url || `jobs/${encodeURIComponent(job.id)}/artifacts/${encodeURIComponent(artifact.name)}`, apiBase);
        if (url.origin !== window.location.origin || !["http:", "https:"].includes(url.protocol)) return;
        const link = document.createElement("a");
        link.className = "web-download";
        link.href = url.href;
        link.download = "";
        const label = document.createElement("span");
        label.textContent = artifact.label || artifact.name || "Ergebnis herunterladen";
        const arrow = document.createElement("span");
        arrow.textContent = "↓";
        arrow.setAttribute("aria-hidden", "true");
        link.append(label, arrow);
        byId("downloads").append(link);
      });
      if (!byId("downloads").childElementCount) showError("job-error", "Die Download-Dateien konnten nicht geladen werden. Lade den Status erneut.");
      byId("refresh-job").hidden = byId("downloads").childElementCount > 0;
      setStep("step-download");
    } else setStep(["queued", "running"].includes(job.status) ? "step-download" : byId("video-file").files.length ? "step-language" : "step-file");
    renderRecent();
  }

  function schedulePoll(id, revision) {
    if (!state.authenticated || state.activeJob?.id !== id || state.jobRevision !== revision) return;
    state.pollTimer = setTimeout(() => pollJob(id, revision), 2500);
  }

  async function pollJob(id, revision) {
    if (!state.authenticated || state.activeJob?.id !== id || state.jobRevision !== revision) return;
    const controller = new AbortController();
    state.pollController = controller;
    let completed = false;
    try {
      const job = await api(`jobs/${encodeURIComponent(id)}`, { signal: controller.signal });
      if (state.jobRevision !== revision || state.activeJob?.id !== id) return;
      renderJob(job);
      completed = terminalStates.has(job.status);
      if (completed) await loadRecent();
      else if (Date.now() - state.lastCapabilitiesAt > 30000) {
        applyCapabilities(await api("capabilities", { signal: controller.signal }));
      }
    } catch (error) {
      if (error.name === "AbortError" || state.jobRevision !== revision) return;
      if (state.authenticated) {
        showError("job-error", `${error.message} Dein gespeicherter Auftrag bleibt erhalten.`);
        byId("refresh-job").hidden = false;
        if ([403, 404, 410].includes(error.status)) completed = true;
      }
    } finally {
      if (state.pollController === controller) state.pollController = null;
      if (!completed) schedulePoll(id, revision);
    }
  }

  function selectJob(job, scroll = false) {
    stopPolling();
    state.selectionCleared = false;
    renderJob(job);
    if (!terminalStates.has(job.status)) pollJob(job.id, state.jobRevision);
    if (scroll) byId("job-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderRecent() {
    byId("recent-jobs").replaceChildren();
    byId("recent-panel").hidden = state.recentJobs.length === 0;
    state.recentJobs.slice(0, 10).forEach((snapshot) => {
      const job = state.activeJob?.id === snapshot.id ? state.activeJob : snapshot;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "web-recent-job";
      button.disabled = state.uploading;
      if (job.id === state.activeJob?.id) button.setAttribute("aria-current", "true");
      const name = document.createElement("span");
      name.textContent = filenameFor(job);
      const meta = document.createElement("span");
      meta.className = "web-recent-meta";
      meta.textContent = statusLabels[job.status] || "Auftrag";
      if (job.created_at) {
        const date = new Date(typeof job.created_at === "number" ? job.created_at * 1000 : job.created_at);
        if (!Number.isNaN(date.valueOf())) {
          const time = document.createElement("time");
          time.dateTime = date.toISOString();
          time.textContent = date.toLocaleString("de", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
          meta.append(time);
        }
      }
      button.append(name, meta);
      button.addEventListener("click", () => {
        selectJob(job, true);
        if (terminalStates.has(job.status)) pollJob(job.id, state.jobRevision);
      });
      byId("recent-jobs").append(button);
    });
  }

  async function loadRecent(resume = false) {
    const generation = state.generation;
    byId("refresh-recent").disabled = true;
    try {
      const data = await api("jobs");
      if (generation !== state.generation || !state.authenticated) return;
      state.recentJobs = Array.isArray(data.jobs) ? data.jobs : [];
      renderRecent();
      if (resume && !state.activeJob && !state.selectionCleared && state.recentJobs.length) {
        selectJob(state.recentJobs.find((job) => !terminalStates.has(job.status)) || state.recentJobs[0]);
      }
    } catch (error) {
      if (state.authenticated) notice(`Deine letzten Aufträge konnten nicht geladen werden. ${error.message}`, "warning", true);
    } finally {
      byId("refresh-recent").disabled = false;
    }
  }

  function setUploading(uploading) {
    state.uploading = uploading;
    byId("upload-fields").disabled = uploading;
    byId("upload-submit").disabled = uploading || !state.capabilities?.engines?.length;
    byId("upload-submit").textContent = uploading ? "Video wird hochgeladen …" : "Übersetzung starten →";
    byId("prepare-job").disabled = uploading;
    byId("refresh-recent").disabled = uploading;
    renderRecent();
  }

  const maxChunkBytes = 8 * 1024 * 1024;

  function cancelledUpload() {
    return new ApiError("Upload abgebrochen.", -1);
  }

  function checkUpload(operation) {
    if (operation.cancelled || operation.controller.signal.aborted) throw cancelledUpload();
  }

  function transientUploadError(error) {
    return error.status === 0 || error.status === 429 || error.status >= 500;
  }

  function uploadDelay(milliseconds, operation) {
    checkUpload(operation);
    return new Promise((resolve, reject) => {
      const abort = () => { clearTimeout(timer); reject(cancelledUpload()); };
      const timer = setTimeout(() => {
        operation.controller.signal.removeEventListener("abort", abort);
        resolve();
      }, milliseconds);
      operation.controller.signal.addEventListener("abort", abort, { once: true });
    });
  }

  async function uploadJson(path, options = {}, operation = null) {
    if (operation) checkUpload(operation);
    const controller = new AbortController();
    const abort = () => controller.abort();
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 30000);
    operation?.controller.signal.addEventListener("abort", abort, { once: true });
    try {
      return await api(path, { ...options, signal: controller.signal });
    } catch (error) {
      if (operation?.cancelled) throw cancelledUpload();
      if (timedOut) throw new ApiError("Die Verbindung antwortet gerade nicht. Der Upload wird geprüft.");
      throw error;
    } finally {
      clearTimeout(timeout);
      operation?.controller.signal.removeEventListener("abort", abort);
    }
  }

  async function retryUploadJson(path, options, operation) {
    for (let attempt = 0; ; attempt += 1) {
      try { return await uploadJson(path, options, operation); }
      catch (error) {
        if (!transientUploadError(error) || attempt >= 3) throw error;
        byId("job-stage").textContent = "Verbindung unterbrochen. Der Upload wird fortgesetzt …";
        await uploadDelay(1000 * 2 ** attempt, operation);
      }
    }
  }

  function uploadRequestId() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
  }

  function checkedUploadSession(row, operation, file) {
    if (!row || typeof row.id !== "string" || !row.id || row.id.length > 200
      || typeof row.job_id !== "string" || !row.job_id || row.job_id.length > 200
      || (operation.id && row.id !== operation.id)
      || (operation.jobId && row.job_id !== operation.jobId)
      || !Number.isSafeInteger(row.offset) || row.offset < 0 || row.offset > file.size
      || !Number.isSafeInteger(row.chunk_bytes) || row.chunk_bytes < 1 || row.chunk_bytes > maxChunkBytes
      || (row.size_bytes !== undefined && row.size_bytes !== file.size)
      || typeof row.status !== "string") {
      throw new ApiError("Der Upload konnte nicht sicher bestätigt werden. Bitte versuche es erneut.", 422);
    }
    operation.id = row.id;
    operation.jobId = row.job_id;
    operation.session = row;
    if (["failed", "expired"].includes(row.status)) throw new ApiError(row.error || "Der Upload konnte nicht abgeschlossen werden.", 422);
    if (["cancelled", "aborted"].includes(row.status)) throw cancelledUpload();
    return row;
  }

  function uploadProgress(offset, inFlight, size) {
    const bytes = Math.min(size, Math.max(0, offset + inFlight));
    setProgress(bytes / size * 100);
    byId("job-stage").textContent = bytes === size
      ? "Upload angekommen. Deine Datei wird geprüft …"
      : `${formatBytes(bytes)} von ${formatBytes(size)} hochgeladen`;
  }

  function sendUploadChunk(operation, chunk, offset, checksum, fileSize) {
    checkUpload(operation);
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      operation.request = request;
      const abort = () => request.abort();
      const finish = (callback, value) => {
        operation.controller.signal.removeEventListener("abort", abort);
        if (operation.request === request) operation.request = null;
        callback(value);
      };
      request.open("PUT", new URL(`uploads/${encodeURIComponent(operation.id)}?offset=${offset}`, apiBase));
      request.withCredentials = true;
      request.timeout = 90000;
      request.setRequestHeader("Content-Type", "application/octet-stream");
      request.setRequestHeader("X-LinguoAI-Request", "1");
      request.setRequestHeader("X-Chunk-SHA256", checksum);
      request.upload.addEventListener("progress", (event) => {
        if (!operation.cancelled && event.lengthComputable) uploadProgress(offset, Math.min(chunk.size, event.loaded), fileSize);
      });
      request.addEventListener("load", () => {
        let row;
        try { row = JSON.parse(request.responseText); }
        catch (_error) { finish(reject, new ApiError("Die Upload-Bestätigung wurde unterbrochen.", request.status >= 400 ? request.status : 502)); return; }
        if (request.status >= 200 && request.status < 300) finish(resolve, row);
        else {
          if (request.status === 401 && state.authenticated) expireSession();
          finish(reject, new ApiError(errorDetail(row, "Ein Teil der Datei konnte nicht übertragen werden."), request.status));
        }
      });
      request.addEventListener("error", () => finish(reject, new ApiError("Die Verbindung beim Upload wurde unterbrochen.")));
      request.addEventListener("timeout", () => finish(reject, new ApiError("Die Übertragung hat zu lange nicht geantwortet.")));
      request.addEventListener("abort", () => finish(reject, cancelledUpload()));
      operation.controller.signal.addEventListener("abort", abort, { once: true });
      request.send(chunk);
    });
  }

  function beginUpload() {
    const operation = {
      controller: new AbortController(), request: null, id: null, jobId: null,
      session: null, completed: false, cancelled: false, deletePromise: null, cleanupError: null, cancelledJob: null,
    };
    operation.abort = () => {
      operation.cancelled = true;
      operation.controller.abort();
      operation.request?.abort();
      if (!operation.id) return Promise.resolve(null);
      if (!operation.deletePromise) {
        operation.deletePromise = uploadJson(`uploads/${encodeURIComponent(operation.id)}`, { method: "DELETE" })
          .then((row) => { operation.cancelledJob = row?.job || null; return row; })
          .catch((error) => { operation.cleanupError = error; return null; });
      }
      return operation.deletePromise;
    };
    state.upload = operation;
    return operation;
  }

  async function uploadFile(file, options) {
    if (!crypto?.subtle || !crypto.getRandomValues) {
      throw new ApiError("Öffne den Dienst über eine sichere HTTPS-Adresse, um dein Video hochzuladen.", 422);
    }
    const operation = beginUpload();
    try {
      const requestId = uploadRequestId();
      let session = checkedUploadSession(await retryUploadJson("uploads", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Upload-Request-ID": requestId },
        body: JSON.stringify({ filename: file.name, size_bytes: file.size, options }),
      }, operation), operation, file);
      checkUpload(operation);
      uploadProgress(session.offset, 0, file.size);
      while (session.offset < file.size) {
        checkUpload(operation);
        const offset = session.offset;
        const chunk = file.slice(offset, Math.min(file.size, offset + session.chunk_bytes));
        byId("job-stage").textContent = "Nächster Dateiteil wird für den Upload geprüft …";
        const digest = await crypto.subtle.digest("SHA-256", await chunk.arrayBuffer());
        const checksum = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
        checkUpload(operation);
        for (let attempt = 0; ; attempt += 1) {
          try {
            session = checkedUploadSession(await sendUploadChunk(operation, chunk, offset, checksum, file.size), operation, file);
            if (session.offset !== offset + chunk.size) throw new ApiError("Der gespeicherte Dateistand wurde nicht korrekt bestätigt.", 422);
            break;
          } catch (error) {
            checkUpload(operation);
            if (error.status === 409) {
              const recovered = checkedUploadSession(await retryUploadJson(`uploads/${encodeURIComponent(operation.id)}`, {}, operation), operation, file);
              if (recovered.offset >= offset + chunk.size) { session = recovered; break; }
              throw error;
            }
            if (!transientUploadError(error) || attempt >= 3) throw error;
            byId("job-stage").textContent = "Verbindung unterbrochen. Der gespeicherte Dateistand wird geprüft …";
            await uploadDelay(1000 * 2 ** attempt, operation);
            const recovered = checkedUploadSession(await retryUploadJson(`uploads/${encodeURIComponent(operation.id)}`, {}, operation), operation, file);
            if (recovered.offset >= offset + chunk.size) { session = recovered; break; }
            if (recovered.offset !== offset) { session = recovered; break; }
            uploadProgress(recovered.offset, 0, file.size);
          }
        }
        uploadProgress(session.offset, 0, file.size);
      }
      checkUpload(operation);
      byId("job-stage").textContent = "Datei wird gespeichert und dein Auftrag vorbereitet …";
      session = checkedUploadSession(await retryUploadJson(`uploads/${encodeURIComponent(operation.id)}/complete`, { method: "POST" }, operation), operation, file);
      while (session.status !== "completed") {
        checkUpload(operation);
        if (session.status !== "finalizing") throw new ApiError("Die Datei konnte nicht für die Verarbeitung freigegeben werden.", 422);
        await uploadDelay(1500, operation);
        session = checkedUploadSession(await retryUploadJson(`uploads/${encodeURIComponent(operation.id)}`, {}, operation), operation, file);
      }
      operation.completed = true;
      checkUpload(operation);
      return session.job || await retryUploadJson(`jobs/${encodeURIComponent(operation.jobId)}`, {}, operation);
    } catch (error) {
      const cancelled = operation.cancelled || error.status === -1;
      if (!operation.completed || cancelled) await operation.abort();
      if (cancelled) {
        const result = cancelledUpload();
        result.job = operation.cancelledJob;
        if (operation.cleanupError) result.cleanupWarning = "Der Upload ist beendet. Der Abbruch konnte noch nicht bestätigt werden. Prüfe deine letzten Aufträge.";
        throw result;
      }
      throw error;
    }
  }

  byId("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    showError("login-error");
    byId("login-submit").disabled = true;
    try {
      await api("auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        username: byId("username").value.trim(), password: byId("password").value,
      }) });
      byId("password").value = "";
      applyCapabilities(await api("capabilities"), true);
      if (state.authenticated) { await loadRecent(true); byId("video-file").focus(); }
    } catch (error) { showError("login-error", error.message); }
    finally { byId("login-submit").disabled = false; }
  });

  byId("logout").addEventListener("click", async () => {
    byId("logout").disabled = true;
    try {
      if (state.uploading && state.upload) await state.upload.abort();
      await api("auth/logout", { method: "POST" });
      setAuthenticated(false);
      notice("Du bist abgemeldet.");
    }
    catch (error) { notice(error.message, "error", true); }
    finally { byId("logout").disabled = false; }
  });

  function validateFile(file) {
    if (!/\.(mp4|mkv|mov|avi|webm|m4v)$/i.test(file.name)) return "Wähle ein Video im Format MP4, MKV, MOV, AVI, WebM oder M4V.";
    if (!file.size) return "Die ausgewählte Datei ist leer.";
    if (state.capabilities?.max_upload_bytes && file.size > state.capabilities.max_upload_bytes) {
      return `Die Datei ist zu groß. Erlaubt sind maximal ${formatBytes(state.capabilities.max_upload_bytes)}.`;
    }
    return "";
  }

  function updateSelectedFile() {
    const file = byId("video-file").files[0];
    byId("file-info").textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "Noch keine Datei ausgewählt.";
    byId("file-drop-zone").classList.toggle("has-file", Boolean(file));
    showError("upload-error", file ? validateFile(file) : "");
    setStep(file ? "step-language" : "step-file");
  }

  byId("video-file").addEventListener("change", updateSelectedFile);
  const dropZone = byId("file-drop-zone");
  let dragDepth = 0;
  dropZone.addEventListener("dragenter", (event) => {
    event.preventDefault();
    if (state.uploading || !state.authenticated) return;
    dragDepth += 1;
    dropZone.classList.add("is-dragging");
  });
  dropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = state.uploading ? "none" : "copy";
  });
  dropZone.addEventListener("dragleave", (event) => {
    event.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) dropZone.classList.remove("is-dragging");
  });
  dropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    dragDepth = 0;
    dropZone.classList.remove("is-dragging");
    if (state.uploading || !state.authenticated) return;
    const files = event.dataTransfer?.files;
    if (!files?.length) return;
    if (files.length !== 1) { showError("upload-error", "Bitte lege genau ein Video pro Auftrag ab."); return; }
    const error = validateFile(files[0]);
    if (error) { showError("upload-error", error); return; }
    try {
      const transfer = new DataTransfer();
      transfer.items.add(files[0]);
      byId("video-file").files = transfer.files;
      updateSelectedFile();
    } catch (_error) {
      showError("upload-error", "Bitte wähle dein Video über die Dateiauswahl aus.");
    }
  });
  byId("asr-engine").addEventListener("change", updateSourceLanguages);
  byId("parakeet-profile").addEventListener("change", updateSourceLanguages);
  byId("translation-service").addEventListener("change", updateServiceNote);
  byId("tts-engine").addEventListener("change", updateServiceNote);

  byId("upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.uploading || !state.authenticated) return;
    showError("upload-error");
    const file = byId("video-file").files[0];
    if (!file) { showError("upload-error", "Wähle zuerst ein Video aus."); return; }
    const fileError = validateFile(file);
    if (fileError) { showError("upload-error", fileError); return; }
    const options = {
      target_language: byId("target-language").value,
      source_language: byId("source-language").value || null,
      asr_engine: byId("asr-engine").value,
      parakeet_profile: byId("parakeet-profile").value || "multilingual-v3",
      translation_service: byId("translation-service").value,
      tts_engine: byId("tts-engine").value,
      tts_voice: byId("tts-engine").value === "edge" ? (byId("tts-voice").value.trim() || null) : null,
    };
    if (options.asr_engine === "parakeet" && !options.source_language) { showError("upload-error", "Wähle die Originalsprache für Parakeet."); return; }
    const generation = state.generation;
    stopPolling();
    state.activeJob = null;
    state.filename = file.name;
    byId("job-panel").hidden = false;
    byId("job-panel").dataset.status = "uploading";
    byId("result-preview").hidden = true;
    byId("job-timing").hidden = true;
    byId("job-heading").textContent = "Dein Video wird hochgeladen";
    byId("job-filename").textContent = file.name;
    byId("job-status").textContent = "Upload";
    byId("job-stage").textContent = "Upload wird gestartet …";
    byId("job-note").textContent = "Lass diese Seite während des Uploads geöffnet.";
    byId("downloads").replaceChildren();
    showError("job-error");
    byId("cancel-job").hidden = false;
    byId("cancel-job").disabled = false;
    byId("cancel-job").textContent = "Upload abbrechen";
    byId("refresh-job").hidden = true;
    byId("prepare-job").hidden = true;
    setProgress(0);
    setUploading(true);
    let uploadOperation;
    try {
      const pendingUpload = uploadFile(file, options);
      uploadOperation = state.upload;
      const job = await pendingUpload;
      if (generation !== state.generation) return;
      if (!job?.id) throw new ApiError("Der Upload wurde nicht bestätigt. Lade deine letzten Aufträge erneut.");
      if (!job.filename) job.filename = file.name;
      selectJob(job, true);
      await loadRecent();
    } catch (error) {
      if (generation !== state.generation) return;
      if (error.status === 401) { expireSession(); return; }
      if (error.status === -1 && error.job?.id) {
        selectJob(error.job, true);
        await loadRecent();
        return;
      }
      byId("job-heading").textContent = error.status === -1 ? "Upload abgebrochen" : "Upload konnte nicht abgeschlossen werden";
      byId("job-status").textContent = error.status === -1 ? "Abgebrochen" : "Upload fehlgeschlagen";
      byId("job-panel").dataset.status = error.status === -1 ? "cancelled" : "failed";
      byId("job-stage").textContent = "";
      byId("job-note").textContent = "Du kannst die Datei erneut hochladen. Prüfe zuvor deine letzten Aufträge, falls der Upload bereits vollständig angekommen war.";
      byId("cancel-job").hidden = true;
      byId("prepare-job").hidden = false;
      showError("job-error", error.status === -1 ? (error.cleanupWarning || "") : error.message);
      await loadRecent();
    } finally {
      if (state.upload === uploadOperation) {
        state.upload = null;
        setUploading(false);
      }
    }
  });

  byId("cancel-job").addEventListener("click", async () => {
    if (state.uploading && state.upload) { state.upload.abort(); return; }
    const id = state.activeJob?.id;
    if (!id) return;
    byId("cancel-job").disabled = true;
    try {
      const job = await api(`jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      if (state.activeJob?.id === id) {
        if (job?.id) selectJob(job);
        else { state.activeJob.cancel_requested = true; renderJob(state.activeJob); }
      }
    } catch (error) {
      if (state.activeJob?.id === id) { showError("job-error", error.message); byId("cancel-job").disabled = false; }
    }
  });

  byId("prepare-job").addEventListener("click", () => {
    if (state.uploading || !state.authenticated) return;
    stopPolling();
    state.activeJob = null;
    state.filename = "";
    state.selectionCleared = true;
    byId("job-panel").hidden = true;
    byId("job-timing").hidden = true;
    byId("result-preview").hidden = false;
    byId("downloads").replaceChildren();
    byId("video-file").value = "";
    showError("job-error");
    updateSelectedFile();
    renderRecent();
    byId("video-file").focus();
  });

  byId("refresh-job").addEventListener("click", () => {
    if (!state.activeJob?.id) return;
    const id = state.activeJob.id;
    stopPolling();
    pollJob(id, state.jobRevision);
  });
  byId("refresh-recent").addEventListener("click", () => loadRecent());
  byId("reconnect").addEventListener("click", connect);
  window.addEventListener("pagehide", stopPolling);
  window.addEventListener("pageshow", async (event) => {
    if (!event.persisted) return;
    await connect();
    if (state.authenticated && state.activeJob?.id) pollJob(state.activeJob.id, state.jobRevision);
  });
  connect();
})();
