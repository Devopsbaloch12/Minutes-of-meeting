/* MoM AI — no build step, no CDN. */

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content || '';
}

/* ── Toasts ─────────────────────────────────────────────── */

const TOAST_ICON = {
  success: 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z',
  error: 'M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
  warning: 'M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z',
  info: 'M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
};

function showToast(message, category = 'info', duration = 5000) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const type = category === 'danger' ? 'error' : (TOAST_ICON[category] ? category : 'info');

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <svg class="toast-icon toast-icon-${type}" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="${TOAST_ICON[type]}"/>
    </svg>
    <p class="toast-message"></p>
    <button class="toast-close" aria-label="Dismiss">
      <svg style="width:1rem;height:1rem" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
      </svg>
    </button>`;
  toast.querySelector('.toast-message').textContent = message;

  const dismiss = () => {
    clearTimeout(toast._timer);
    toast.classList.add('toast-out');
    setTimeout(() => toast.remove(), 200);
  };
  toast.querySelector('.toast-close').addEventListener('click', dismiss);
  container.appendChild(toast);
  toast._timer = setTimeout(dismiss, duration);
}

document.addEventListener('DOMContentLoaded', () => {
  const flash = document.getElementById('flash-data');
  if (!flash) return;
  for (const [category, message] of JSON.parse(flash.textContent)) showToast(message, category);
});

/* ── Upload ─────────────────────────────────────────────── */

// First supported wins. Opus in WebM is what Chrome, Edge and Firefox record;
// Safari only records MP4/AAC. Every extension here is one the server accepts.
const RECORDING_FORMATS = [
  ['audio/webm;codecs=opus', 'webm'],
  ['audio/ogg;codecs=opus', 'ogg'],
  ['audio/mp4', 'm4a'],
];

function uploadForm() {
  // MediaRecorder, streams and the audio graph stay out of Alpine's reactive
  // data: wrapping native objects in its proxy breaks their methods.
  const session = { recorder: null, stream: null, chunks: [], audio: null, timer: null,
                    elapsed: 0, resumedAt: 0, wakeLock: null, unsaved: false };

  return {
    allowed: [],
    maxMb: 0,
    file: null,
    dragging: false,
    uploading: false,
    percent: 0,
    recorder: { supported: false, unavailable: '', state: 'idle', clock: '0:00', status: '', level: 0, url: '' },

    get sizeLabel() {
      return this.file ? formatBytes(this.file.size) : '';
    },

    handleDrop(event) {
      this.dragging = false;
      if (event.dataTransfer.files.length) this.setFile(event.dataTransfer.files[0]);
    },

    setFile(file) {
      if (!file) return;
      const extension = (file.name.split('.').pop() || '').toLowerCase();
      if (!this.allowed.includes(extension)) {
        showToast(`That file type is not supported. Allowed: ${this.allowed.join(', ')}.`, 'error');
        return;
      }
      if (file.size > this.maxMb * 1024 * 1024) {
        showToast(`That file is ${formatBytes(file.size)}. The limit is ${this.maxMb} MB.`, 'error');
        return;
      }
      this.file = file;

      const transfer = new DataTransfer();
      transfer.items.add(file);
      this.$refs.fileInput.files = transfer.files;

      const title = document.getElementById('title');
      if (title && !title.value) title.value = file.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ');
    },

    clear() {
      this.file = null;
      this.$refs.fileInput.value = '';
      this.discardRecording();
    },

    init() {
      this.allowed = (this.$el.dataset.allowed || '').split(',').filter(Boolean);
      this.maxMb = Number(this.$el.dataset.maxMb) || 0;
      this.$el.addEventListener('submit', (event) => {
        if (!this.file || this.uploading || this.recorder.state !== 'idle') return;
        event.preventDefault();
        this.send();
      });

      if (!window.isSecureContext) {
        this.recorder.unavailable = 'Browsers only allow the microphone on secure pages. Open this app '
          + 'over https:// (or on localhost) to record here; uploading a file works as usual.';
      } else if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        this.recorder.unavailable = 'This browser cannot record audio. Use a current Chrome, Edge, Firefox or Safari.';
      } else {
        this.recorder.supported = true;
      }
      window.addEventListener('beforeunload', (event) => {
        if (this.recorder.state !== 'idle' || session.unsaved) {
          event.preventDefault();
          event.returnValue = '';
        }
      });
    },

    /* ── Recording ── */

    async startRecording() {
      if (session.unsaved && !window.confirm('Discard the recording that has not been uploaded yet?')) return;

      let stream;
      try {
        // Meeting audio, not a call: echo cancellation would erase remote participants
        // playing through the room's speakers, and noise suppression mutes distant voices.
        stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: true },
        });
      } catch (error) {
        showToast(error.name === 'NotAllowedError'
          ? 'Microphone access was blocked. Allow it from the icon in the address bar, then try again.'
          : `The microphone could not be opened: ${error.message || error.name}`, 'error', 9000);
        return;
      }

      const format = RECORDING_FORMATS.find(([type]) => MediaRecorder.isTypeSupported(type));
      const recorder = new MediaRecorder(stream, format ? { mimeType: format[0], audioBitsPerSecond: 64000 } : {});
      this.discardRecording();
      Object.assign(session, { recorder, stream, chunks: [], elapsed: 0, resumedAt: performance.now() });

      recorder.addEventListener('dataavailable', (event) => {
        if (event.data.size) session.chunks.push(event.data);
      });
      recorder.addEventListener('stop', () => this.finishRecording(format ? format[1] : extensionFor(recorder.mimeType)));
      // A microphone unplugged or revoked mid-meeting: keep what was captured.
      stream.getAudioTracks().forEach((track) => track.addEventListener('ended', () => this.stopRecording()));
      recorder.start(1000);

      this.watchLevel(stream);
      session.timer = setInterval(() => this.tick(), 250);
      navigator.wakeLock?.request('screen').then((lock) => { session.wakeLock = lock; }).catch(() => {});
      this.recorder.state = 'recording';
      this.recorder.status = 'Recording';
      this.tick();
    },

    togglePause() {
      const { recorder } = session;
      if (!recorder) return;
      if (recorder.state === 'recording') {
        recorder.pause();
        session.elapsed += performance.now() - session.resumedAt;
        this.recorder.state = 'paused';
        this.recorder.status = 'Paused';
      } else if (recorder.state === 'paused') {
        recorder.resume();
        session.resumedAt = performance.now();
        this.recorder.state = 'recording';
        this.recorder.status = 'Recording';
      }
    },

    stopRecording() {
      const { recorder } = session;
      if (!recorder || recorder.state === 'inactive') return;
      if (recorder.state === 'recording') session.elapsed += performance.now() - session.resumedAt;
      recorder.stop();  // finishRecording runs once the last chunk has arrived
    },

    finishRecording(extension) {
      clearInterval(session.timer);
      session.stream?.getTracks().forEach((track) => track.stop());
      session.audio?.close();
      session.wakeLock?.release().catch(() => {});
      Object.assign(session, { recorder: null, stream: null, audio: null, wakeLock: null });
      this.recorder.state = 'idle';
      this.recorder.level = 0;

      const blob = new Blob(session.chunks, { type: session.chunks[0]?.type || 'audio/webm' });
      session.chunks = [];
      if (!blob.size) {
        this.recorder.status = '';
        showToast('Nothing was recorded. Check that the right microphone is selected.', 'error');
        return;
      }

      const now = new Date();
      const stamp = now.toISOString().slice(0, 16).replace('T', '-').replace(':', '');
      const title = document.getElementById('title');
      if (title && !title.value) {
        title.value = `Meeting recorded ${now.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}`;
      }
      this.setFile(new File([blob], `recording-${stamp}.${extension}`, { type: blob.type }));
      if (this.file?.size !== blob.size) return;  // refused by setFile, which said why

      this.recorder.url = URL.createObjectURL(blob);
      this.recorder.status = 'Ready to upload';
      session.unsaved = true;
    },

    discardRecording() {
      if (this.recorder.url) URL.revokeObjectURL(this.recorder.url);
      this.recorder.url = '';
      this.recorder.status = '';
      this.recorder.clock = '0:00';
      session.unsaved = false;
    },

    tick() {
      const running = this.recorder.state === 'recording' ? performance.now() - session.resumedAt : 0;
      const seconds = Math.floor((session.elapsed + running) / 1000);
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      const s = String(seconds % 60).padStart(2, '0');
      this.recorder.clock = h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
    },

    watchLevel(stream) {
      // The meter is how a user sees, before an hour is lost, that the wrong microphone is live.
      try {
        const audio = new AudioContext();
        const analyser = audio.createAnalyser();
        analyser.fftSize = 1024;
        audio.createMediaStreamSource(stream).connect(analyser);
        session.audio = audio;
        const samples = new Uint8Array(analyser.fftSize);
        const draw = () => {
          if (session.audio !== audio) return;
          analyser.getByteTimeDomainData(samples);
          let sum = 0;
          for (const value of samples) sum += ((value - 128) / 128) ** 2;
          const level = Math.min(100, Math.round(Math.sqrt(sum / samples.length) * 300));
          this.recorder.level = this.recorder.state === 'paused' ? 0 : level;
          requestAnimationFrame(draw);
        };
        draw();
      } catch {
        // No meter is better than no recording.
      }
    },

    send() {
      this.uploading = true;
      this.percent = 0;

      let lastActivity = Date.now();
      let stalled = false;
      const request = new XMLHttpRequest();
      const watchdog = setInterval(() => {
        if (Date.now() - lastActivity < 120_000) return;
        stalled = true;
        clearInterval(watchdog);
        request.abort();
        this.uploading = false;
        showToast('Upload stalled for two minutes. Check your connection and try again.', 'error');
      }, 5_000);
      const stopWatchdog = () => clearInterval(watchdog);

      request.open('POST', this.$el.action);
      request.upload.addEventListener('progress', (event) => {
        lastActivity = Date.now();
        if (event.lengthComputable) this.percent = Math.round((event.loaded / event.total) * 100);
      });
      request.addEventListener('load', () => {
        stopWatchdog();
        if (request.status >= 200 && request.status < 400) {
          session.unsaved = false;  // uploaded: leaving the page no longer loses anything
          window.location = request.responseURL || '/meetings/';
        } else {
          this.uploading = false;
          showToast(
            request.status === 413
              ? `That file is larger than the ${this.maxMb} MB limit.`
              : `Upload failed (HTTP ${request.status}).`,
            'error',
          );
        }
      });
      request.addEventListener('error', () => {
        stopWatchdog();
        if (stalled) return;
        this.uploading = false;
        showToast('Upload failed. Check that the app is still running.', 'error');
      });
      request.addEventListener('abort', () => {
        stopWatchdog();
        this.uploading = false;
      });
      request.send(new FormData(this.$el));
    },
  };
}

function extensionFor(mimeType) {
  if (/mp4|aac/.test(mimeType)) return 'm4a';
  if (/ogg/.test(mimeType)) return 'ogg';
  return 'webm';
}

function formatBytes(bytes) {
  if (!bytes) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
}

/* ── Status polling ─────────────────────────────────────── */

function pollStatuses(ids, statuses) {
  const active = ids.filter((_, index) => !TERMINAL.has(statuses[index]));
  if (!active.length) return;

  const pending = new Set(active);
  const timer = setInterval(async () => {
    for (const id of [...pending]) {
      const data = await fetchStatus(id);
      if (!data) continue;

      const badge = document.querySelector(`[data-status-badge="${id}"]`);
      if (badge) {
        badge.textContent = capitalise(data.status);
        badge.className = `status-badge status-${data.status}`;
      }
      if (TERMINAL.has(data.status)) pending.delete(id);
    }
    if (!pending.size) clearInterval(timer);
  }, 4000);
}

function trackMeeting(id, status) {
  if (TERMINAL.has(status)) return;

  const timer = setInterval(async () => {
    const data = await fetchStatus(id);
    if (!data) return;

    setText('progress-pct', `${data.progress}%`);
    setText('progress-label', `${capitalise(data.status)}…`);
    setText('status-label', capitalise(data.status));

    const bar = document.getElementById('progress-bar');
    if (bar) bar.style.width = `${data.progress}%`;

    const badge = document.getElementById('status-badge');
    if (badge) badge.className = `status-badge status-${data.status} text-sm px-3 py-1.5`;

    renderEvents(data.events);

    if (TERMINAL.has(data.status)) {
      clearInterval(timer);
      setTimeout(() => window.location.reload(), 700);
    }
  }, 3000);
}

async function fetchStatus(id) {
  try {
    const response = await fetch(`/api/v1/meetings/${id}/status`);
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

function renderEvents(events) {
  const list = document.getElementById('event-list');
  if (!list || !events) return;

  list.replaceChildren(...events.map((event) => {
    const item = document.createElement('li');
    item.className = 'flex items-start gap-3 px-6 py-3';

    const dot = document.createElement('span');
    dot.className = `event-dot event-${event.level}`;

    const message = document.createElement('span');
    message.className = `text-sm flex-1 ${event.level === 'error' ? 'text-red-600' : 'text-gray-700'}`;
    message.textContent = event.message;

    const time = document.createElement('span');
    time.className = 'text-xs text-gray-400 flex-shrink-0';
    time.textContent = event.at ? new Date(event.at).toLocaleTimeString() : '';

    item.append(dot, message, time);
    return item;
  }));
}

/* ── Settings helpers ───────────────────────────────────── */

function applyPreset(select, urlField, modelField) {
  const option = select.selectedOptions[0];
  if (!option?.value) return;
  document.getElementById(urlField).value = option.value;

  const model = document.getElementById(modelField);
  if (model && !model.value) model.value = option.dataset.model || '';
}

async function testProvider(group, button) {
  const result = document.querySelector(`[data-test-result="${group}"]`);
  const original = button.innerHTML;
  button.disabled = true;
  button.textContent = 'Testing…';
  result.textContent = '';
  result.className = 'text-sm truncate';

  try {
    const response = await fetch(`/admin/settings/test/${group}`, {
      method: 'POST',
      headers: { 'X-CSRFToken': csrfToken() },
      body: new FormData(button.closest('form')),
    });
    const data = await response.json();
    result.textContent = data.message;
    result.className = `text-sm truncate ${data.ok ? 'text-green-600' : 'text-red-600'}`;
    if (!data.ok) showToast(data.message, 'error', 9000);
  } catch (error) {
    result.textContent = 'The test could not be run.';
    result.className = 'text-sm truncate text-red-600';
  } finally {
    button.disabled = false;
    button.innerHTML = original;
  }
}

function promptPassword(form) {
  const password = window.prompt('New password (at least 8 characters):');
  if (!password) return false;
  form.querySelector('input[name="password"]').value = password;
  return true;
}

/* ── Misc ───────────────────────────────────────────────── */

function copyElement(id) {
  const element = document.getElementById(id);
  if (!element) return;
  navigator.clipboard.writeText(element.innerText || element.textContent)
    .then(() => showToast('Copied to clipboard.', 'success', 2500))
    .catch(() => showToast('Your browser blocked the copy.', 'error'));
}

function setText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

function capitalise(value) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
