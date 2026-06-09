// JARVIS HUD — controlador principal.
// Painéis do dashboard (sistema, processos, logs, arc reactor) +
// chat overlay opcional (toggle), bridge HTTP/SSE com Python e voz.

// ───── refs principais ─────
const $ = (id) => document.getElementById(id);

const log              = $('log');
const statusDot        = $('status-dot');
const statusLabel      = $('status-label');
const aiDot            = $('ai-dot');
const stateLabel       = $('state-label');
const micDot           = $('mic-dot');
const micLabel         = $('mic-label');
const micToggleBtn     = $('mic-toggle');
const ttsToggleBtn     = $('tts-toggle');
const chatToggleBtn    = $('chat-toggle');
const chatPanel        = $('chat-panel');
const chatCloseBtn     = $('chat-close');
const micBtn           = $('mic-btn');
const sendBtn          = $('send-btn');
const textInput        = $('text-input');
const activationOverlay = $('activation-overlay');
const arcStage         = $('arc-stage');
const innerGlow        = $('inner-glow');
const arcCore          = $('arc-core');
const arcLabel         = $('arc-label');

let currentJarvisMsg = null;
let currentJarvisFullText = '';
let busy = false;
let activated = false;

// ───── relógio / uptime / data ─────
const startTime = Date.now();
const pad = (n) => String(n).padStart(2, '0');

function tick() {
  const now = new Date();
  $('clock').textContent =
    `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;

  const ms = Date.now() - startTime;
  const s = Math.floor(ms / 1000);
  $('uptime').textContent =
    `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;

  const days = ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb'];
  const months = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun', 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez'];
  $('date-val').textContent =
    `${days[now.getDay()]} ${now.getDate()} ${months[now.getMonth()]} ${now.getFullYear()}`;
}
setInterval(tick, 1000);
tick();

// ───── arc reactor: ticks, orbits, crosshair ─────
const tg = $('ticks');
for (let i = 0; i < 72; i++) {
  const angle = (i / 72) * Math.PI * 2;
  const r = 49;
  const len = i % 6 === 0 ? 5 : 2.5;
  const x1 = 50 + Math.cos(angle) * r;
  const y1 = 50 + Math.sin(angle) * r;
  const x2 = 50 + Math.cos(angle) * (r - len);
  const y2 = 50 + Math.sin(angle) * (r - len);
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  line.setAttribute('x1', x1); line.setAttribute('y1', y1);
  line.setAttribute('x2', x2); line.setAttribute('y2', y2);
  line.setAttribute('stroke', i % 6 === 0 ? 'rgba(255,180,40,0.7)' : 'rgba(255,180,40,0.35)');
  line.setAttribute('stroke-width', i % 6 === 0 ? '0.8' : '0.4');
  tg.appendChild(line);
}

(function animateOrbits() {
  const stage = arcStage;
  const stageSize = () => stage.offsetWidth;
  const ods = [
    { el: $('od-a'), phase: 0,    speed: 0.0008, radius: 0.415 },
    { el: $('od-b'), phase: 2.09, speed: 0.0014, radius: 0.415 },
    { el: $('od-c'), phase: 4.18, speed: 0.0011, radius: 0.415 },
  ];
  let t = 0;
  function frame() {
    t++;
    const s = stageSize();
    ods.forEach(od => {
      const angle = od.phase + t * od.speed * Math.PI * 2 * 60;
      const r = s * od.radius;
      const x = s / 2 + Math.cos(angle) * r - od.el.offsetWidth / 2;
      const y = s / 2 + Math.sin(angle) * r - od.el.offsetHeight / 2;
      od.el.style.transform = `none`;
      od.el.style.left = x + 'px';
      od.el.style.top  = y + 'px';
    });
    requestAnimationFrame(frame);
  }
  frame();
})();

const crossSvg = $('crosshair-svg');
function buildCrosshair() {
  crossSvg.innerHTML = '';
  const w = crossSvg.clientWidth, h = crossSvg.clientHeight;
  const cx = w / 2, cy = h / 2;
  const dashLine = (x1, y1, x2, y2) => {
    const l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    l.setAttribute('x1', x1); l.setAttribute('y1', y1);
    l.setAttribute('x2', x2); l.setAttribute('y2', y2);
    l.setAttribute('stroke', 'rgba(255,180,40,0.15)');
    l.setAttribute('stroke-width', '0.7');
    l.setAttribute('stroke-dasharray', '4 6');
    crossSvg.appendChild(l);
  };
  dashLine(0, cy, w, cy);
  dashLine(cx, 0, cx, h);
  dashLine(0, 0, w, h);
  dashLine(w, 0, 0, h);
}
buildCrosshair();
window.addEventListener('resize', buildCrosshair);

// ───── partículas de fundo ─────
const canvas = $('particle-canvas');
const ctx = canvas.getContext('2d');
function resizeCanvas() {
  canvas.width  = window.innerWidth;
  canvas.height = window.innerHeight;
}
resizeCanvas();
window.addEventListener('resize', resizeCanvas);

const PARTICLE_COUNT = 90;
const particles = Array.from({ length: PARTICLE_COUNT }, () => ({
  x: Math.random() * window.innerWidth,
  y: Math.random() * window.innerHeight,
  vx: (Math.random() - 0.5) * 0.3,
  vy: (Math.random() - 0.5) * 0.3,
  r:  Math.random() * 1.8 + 0.4,
  alpha: Math.random() * 0.5 + 0.1,
  pulse: Math.random() * Math.PI * 2,
}));
const CONNECTION_DIST = 140;

function drawParticles() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  particles.forEach(p => {
    p.x += p.vx; p.y += p.vy; p.pulse += 0.02;
    if (p.x < 0 || p.x > canvas.width)  p.vx *= -1;
    if (p.y < 0 || p.y > canvas.height) p.vy *= -1;
  });
  for (let i = 0; i < particles.length; i++) {
    for (let j = i + 1; j < particles.length; j++) {
      const dx = particles[i].x - particles[j].x;
      const dy = particles[i].y - particles[j].y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < CONNECTION_DIST) {
        const alpha = (1 - dist / CONNECTION_DIST) * 0.2;
        ctx.beginPath();
        ctx.strokeStyle = `rgba(255,165,30,${alpha})`;
        ctx.lineWidth = 0.6;
        ctx.moveTo(particles[i].x, particles[i].y);
        ctx.lineTo(particles[j].x, particles[j].y);
        ctx.stroke();
      }
    }
  }
  particles.forEach(p => {
    const a = p.alpha * (0.6 + 0.4 * Math.sin(p.pulse));
    ctx.beginPath();
    ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(255,175,40,${a})`;
    ctx.shadowColor = `rgba(255,175,40,${a * 0.6})`;
    ctx.shadowBlur = 6;
    ctx.fill();
  });
  requestAnimationFrame(drawParticles);
}
drawParticles();

// ───── waveform (orientada por nível de áudio) ─────
const wf = $('waveform');
const BARS = 32;
const waveBars = [];
for (let i = 0; i < BARS; i++) {
  const d = document.createElement('div');
  d.className = 'wave-bar';
  d.style.height = '4px';
  wf.appendChild(d);
  waveBars.push(d);
}
let lastAudioLevel = 0;
function rotateWaveform() {
  const baseline = lastAudioLevel * 26 + 2;
  for (let i = 0; i < BARS; i++) {
    const noise = Math.random() * (lastAudioLevel * 14 + 4);
    const h = Math.max(2, Math.min(28, baseline + noise * (Math.random() < 0.5 ? -1 : 1)));
    waveBars[i].style.height = h + 'px';
    waveBars[i].style.opacity = (0.4 + Math.random() * 0.6).toFixed(2);
  }
  lastAudioLevel *= 0.8;  // decay
}
setInterval(rotateWaveform, 180);

// ───── arc reactor: pulse em função do áudio ─────
function setArcPulse(level) {
  // level: 0..1
  const scale = 1 + Math.min(0.35, level * 0.5);
  arcStage.style.setProperty('--arc-pulse', scale.toFixed(3));
}

// ───── eventos do log lateral (esquerda) ─────
function pushSysLog(text, kind = '') {
  const container = $('log-lines');
  // marca todos os anteriores como inativos
  [...container.children].forEach(c => c.classList.remove('active'));
  const div = document.createElement('div');
  div.className = `log-line active ${kind}`.trim();
  div.textContent = text;
  container.insertBefore(div, container.firstChild);
  while (container.children.length > 6) container.removeChild(container.lastChild);
}

// ───── log de IA (direita) ─────
function pushAiLog(text, kind = '') {
  const container = $('ai-log');
  [...container.children].forEach(c => c.classList.remove('active'));
  const div = document.createElement('div');
  div.className = `log-line active ${kind}`.trim();
  div.textContent = text;
  container.insertBefore(div, container.firstChild);
  while (container.children.length > 5) container.removeChild(container.lastChild);
}

// ───── chat overlay (oculto por padrão) ─────
function showChat(force = null) {
  const wantShow = force === null ? chatPanel.classList.contains('hidden') : force;
  chatPanel.classList.toggle('hidden', !wantShow);
  chatToggleBtn.classList.toggle('active', wantShow);
  if (wantShow) textInput.focus();
}

chatToggleBtn.addEventListener('click', () => showChat());
chatCloseBtn.addEventListener('click', () => showChat(false));

// ───── status / state visuais ─────
function setState(state, text) {
  arcStage.dataset.state = state;
  stateLabel.textContent = state.toUpperCase();
  if (text) statusLabel.textContent = text;
  if (arcLabel) {
    arcLabel.dataset.val = state.toUpperCase();
  }
}

function setOnline(ok, msg) {
  statusDot.classList.toggle('error', !ok);
  statusDot.classList.toggle('offline', !ok);
  if (ok) statusDot.classList.remove('offline', 'error');
  statusLabel.textContent = msg || (ok ? 'Sistema Ativo' : 'Offline');
  aiDot.classList.toggle('offline', !ok);
}

function setMicIndicator(on) {
  micDot.classList.toggle('offline', !on);
  micLabel.textContent = on ? 'MIC ON' : 'MIC OFF';
  micBtn.classList.toggle('active', on);
  micToggleBtn.classList.toggle('active', on);
}

// ───── chat: render mensagens ─────
function appendUser(text) {
  const div = document.createElement('div');
  div.className = 'msg user';
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  pushAiLog(`USR: ${truncate(text, 40)}`);
}

function appendJarvisStart() {
  const div = document.createElement('div');
  div.className = 'msg jarvis';
  div.textContent = '';
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  currentJarvisMsg = div;
  currentJarvisFullText = '';
  return div;
}

function appendJarvisToken(text) {
  if (!currentJarvisMsg) appendJarvisStart();
  currentJarvisMsg.textContent += text;
  currentJarvisFullText += text;
  log.scrollTop = log.scrollHeight;
}

function appendJarvisEnd(fullText) {
  const text = fullText || currentJarvisFullText;
  currentJarvisMsg = null;
  currentJarvisFullText = '';
  if (text) pushAiLog(`AI: ${truncate(text, 50)}`);
  if (text && window.voice) {
    setState('speaking', 'falando');
    window.voice.speak(text);
  } else {
    setState('idle', 'pronto');
  }
}

function appendTool(name, args) {
  const div = document.createElement('div');
  div.className = 'msg tool';
  div.textContent = `⚙ ${name}(${formatArgs(args)})`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  pushSysLog(`TOOL: ${name}`, 'tool');
  return div;
}

function appendToolResult(toolDiv, result) {
  if (!toolDiv) return;
  toolDiv.textContent += `\n↳ ${truncate(formatResult(result), 220)}`;
  log.scrollTop = log.scrollHeight;
}

function appendError(text) {
  const div = document.createElement('div');
  div.className = 'msg error';
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  pushSysLog(`ERR: ${truncate(text, 40)}`, 'error');
}

function formatArgs(args) {
  if (!args) return '';
  return Object.entries(args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ');
}

function formatResult(r) {
  if (typeof r === 'string') return r;
  try { return JSON.stringify(r); } catch { return String(r); }
}

function truncate(s, n) {
  if (!s) return '';
  s = String(s);
  return s.length > n ? s.substring(0, n) + '…' : s;
}

// ───── handler de eventos SSE ─────
let lastToolDiv = null;

function handleEvent(ev) {
  switch (ev.type) {
    case 'tool_call':
      lastToolDiv = appendTool(ev.name, ev.args);
      setState('thinking', `executando ${ev.name}`);
      break;
    case 'tool_result':
      appendToolResult(lastToolDiv, ev.result);
      break;
    case 'assistant_start':
      setState('thinking', 'gerando resposta');
      appendJarvisStart();
      break;
    case 'token':
      appendJarvisToken(ev.text);
      break;
    case 'assistant_end':
      appendJarvisEnd(ev.full_text);
      busy = false;
      break;
    case 'error':
      appendError(ev.message);
      setState('error', 'erro');
      busy = false;
      break;
    case 'wake':
      if (window.voice) {
        window.voice.enableAlwaysOn();
        if (ev.message) {
          setState('speaking', 'detectado');
          window.voice.speak(ev.message);
        }
      }
      pushSysLog('SYS: 2 palmas detectadas', 'tool');
      break;
  }
}

// ───── HTTP API ─────
async function send(text) {
  if (!text || busy) return;
  busy = true;
  appendUser(text);
  textInput.value = '';
  setState('thinking', 'enviando');
  try {
    const res = await fetch('/api/send_message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      appendError(data.error || `HTTP ${res.status}`);
      busy = false;
      setState('idle', 'pronto');
    }
  } catch (e) {
    appendError(`falha de rede: ${e}`);
    busy = false;
    setState('error', 'desconectado');
  }
}

async function greet() {
  try { await fetch('/api/greet', { method: 'POST' }); }
  catch (e) { console.warn('greet falhou:', e); }
}

async function checkHealth() {
  try {
    const res = await fetch('/api/health');
    const data = await res.json();
    setOnline(data.ok, data.message);
    setState(data.ok ? 'idle' : 'error', data.ok ? 'pronto' : 'falha');
  } catch (e) {
    setOnline(false, `bridge offline: ${e}`);
  }
}

// ───── poll de stats reais (CPU/RAM/processos/etc) ─────
async function pollStats() {
  try {
    const res = await fetch('/api/stats');
    if (!res.ok) return;
    const data = await res.json();
    renderStats(data);
  } catch (e) {
    /* silencioso — pode acontecer em transições */
  }
}

function renderStats(data) {
  const sys = data.system || {};
  if (sys.cpu_percent !== undefined) {
    $('cpu-val').textContent = sys.cpu_percent.toFixed(0) + '%';
    $('cpu-bar').style.width = Math.min(100, sys.cpu_percent) + '%';
  }
  if (sys.ram_percent !== undefined) {
    $('mem-val').textContent =
      `${(sys.ram_used_gb || 0).toFixed(1)} / ${(sys.ram_total_gb || 0).toFixed(1)} GB`;
    $('mem-bar').style.width = Math.min(100, sys.ram_percent) + '%';
  }
  if (sys.disk_percent !== undefined) {
    $('disk-val').textContent = sys.disk_percent.toFixed(0) + '%';
    $('disk-bar').style.width = Math.min(100, sys.disk_percent) + '%';
  }
  const net = data.net || {};
  if (net.rx_kbps !== undefined) {
    const rx = net.rx_kbps;
    $('net-val').textContent = rx > 1024 ? (rx / 1024).toFixed(1) + ' MB/s' : rx.toFixed(0) + ' KB/s';
    // barra: log-scale até 2 MB/s = 100%
    const pct = Math.min(100, Math.log10(1 + rx) / Math.log10(2049) * 100);
    $('net-bar').style.width = pct + '%';
  }

  // sistema
  if (sys.platform) {
    $('sys-platform').textContent = `${sys.platform} ${sys.platform_release || ''}`.trim();
  }
  if (sys.cpu_count !== undefined) $('sys-cpus').textContent = sys.cpu_count + ' cores';
  if (sys.ram_total_gb !== undefined) $('sys-ram').textContent = sys.ram_total_gb.toFixed(1) + ' GB';
  if (sys.uptime) $('sys-boot').textContent = sys.uptime;

  // temperaturas
  const tempsEl = $('temps-list');
  if (data.temps && Object.keys(data.temps).length) {
    tempsEl.innerHTML = '';
    Object.entries(data.temps).slice(0, 6).forEach(([k, v]) => {
      const row = document.createElement('div');
      row.className = 'metric-row';
      row.innerHTML = `<span class="metric-label">${escapeHtml(k)}</span><span class="metric-value">${v}°C</span>`;
      tempsEl.appendChild(row);
    });
  } else if (!tempsEl.dataset.noSensors) {
    tempsEl.innerHTML =
      '<div class="metric-row"><span class="metric-label">Sem sensores expostos</span><span class="metric-value">—</span></div>';
    tempsEl.dataset.noSensors = '1';
  }

  // processos
  const procEl = $('proc-list');
  if (data.processes && data.processes.length) {
    procEl.innerHTML = '';
    data.processes.slice(0, 6).forEach(p => {
      const row = document.createElement('div');
      row.className = 'metric-row';
      const name = (p.name || '—').replace(/\.exe$/i, '');
      row.innerHTML =
        `<span class="metric-label" title="${escapeHtml(p.name)} (PID ${p.pid})">${escapeHtml(truncate(name, 18))}</span>` +
        `<span class="metric-value">${p.cpu.toFixed(0)}%</span>`;
      procEl.appendChild(row);
    });
  }

  // modelo
  if (data.model) $('model-name').textContent = data.model;

  // memória do processo Python (RAM atual)
  if (sys.ram_used_gb !== undefined) {
    $('ai-mem').textContent = sys.ram_used_gb.toFixed(1) + ' GB';
  }
}

function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ───── SSE ─────
function connectEvents() {
  const es = new EventSource('/api/events');
  es.onopen = () => checkHealth();
  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      handleEvent(data);
    } catch (err) {
      console.warn('SSE parse:', err, e.data);
    }
  };
  es.onerror = () => setOnline(false, 'reconectando...');
}

// ───── ativação ─────
function activate() {
  if (activated) return;
  activated = true;
  activationOverlay.classList.add('gone');

  // "Destrava" o áudio HTML5 dentro do gesto do usuário. Sem isso, o
  // <audio> do TTS local pode falhar com NotAllowedError em Edge --app.
  try {
    const silentWav = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=';
    const unlock = new Audio(silentWav);
    unlock.volume = 0;
    unlock.play().catch(() => {});
  } catch (_) {}

  if (window.speechSynthesis) {
    try {
      const warm = new SpeechSynthesisUtterance(' ');
      warm.volume = 0;
      speechSynthesis.speak(warm);
    } catch (_) {}
  }

  if (window.voice) window.voice.enableAlwaysOn();
  setTimeout(() => greet(), 500);
}
activationOverlay.addEventListener('click', activate);

// ───── inputs ─────
sendBtn.addEventListener('click', () => send(textInput.value.trim()));
textInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send(textInput.value.trim());
  }
});

function toggleMic() {
  if (!window.voice) return;
  if (window.voice.alwaysOn) window.voice.disableAlwaysOn();
  else                       window.voice.enableAlwaysOn();
}
micBtn.addEventListener('click', toggleMic);
micToggleBtn.addEventListener('click', toggleMic);

function toggleTTS() {
  if (!window.voice) return;
  window.voice.toggleMuted();
}
function syncTTSButton(muted) {
  if (!ttsToggleBtn) return;
  ttsToggleBtn.classList.toggle('active', !muted);
  ttsToggleBtn.textContent = muted ? 'TTS OFF' : 'TTS';
  ttsToggleBtn.title = muted
    ? 'JARVIS está mudo — clique para ele falar (Ctrl+T)'
    : 'JARVIS está falando — clique para silenciar (Ctrl+T)';
}
ttsToggleBtn.addEventListener('click', toggleTTS);

window.addEventListener('keydown', (e) => {
  if (e.ctrlKey && (e.key === 'm' || e.key === 'M')) {
    e.preventDefault();
    toggleMic();
    return;
  }
  if (e.ctrlKey && (e.key === 't' || e.key === 'T')) {
    e.preventDefault();
    toggleTTS();
    return;
  }
  if (e.ctrlKey && (e.key === 'l' || e.key === 'L')) {
    e.preventDefault();
    showChat();
    return;
  }
  if (e.key === 'Escape') {
    if (window.voice && window.voice.speaking) {
      e.preventDefault();
      window.voice.cancel();
      setState('idle', 'interrompido');
    }
  }
});

// clicar no core do arc reactor interrompe TTS (atalho visual divertido)
arcCore.addEventListener('click', () => {
  if (!activated) return;
  if (window.voice && window.voice.speaking) {
    window.voice.cancel();
    setState('idle', 'interrompido');
  }
});

// ───── voice callbacks ─────
if (window.voice) {
  window.voice.onAlwaysOnChange = (on) => {
    setMicIndicator(on);
    if (on && !busy) setState('listening', 'ouvindo');
    else if (!on && !busy) setState('idle', 'pronto');
  };
  window.voice.onListenStart = () => { if (!busy) setState('listening', 'ouvindo'); };
  window.voice.onListenEnd   = () => {
    if (!window.voice.alwaysOn && !busy) setState('idle', 'pronto');
  };
  window.voice.onTranscript = (text) => {
    if (window.voice.suspended) return;
    send(text);
  };
  window.voice.onMutedChange = (muted) => syncTTSButton(muted);
  syncTTSButton(window.voice.muted);
  window.voice.onSpeakStart = () => setState('speaking', 'falando');
  window.voice.onSpeakEnd   = () => {
    if (!busy) {
      const on = window.voice.alwaysOn;
      setState(on ? 'listening' : 'idle', on ? 'ouvindo' : 'pronto');
    }
  };
  window.voice.onAudioFrame = (level) => {
    lastAudioLevel = Math.max(lastAudioLevel, level);
    setArcPulse(level);
  };
}

// ───── inicialização ─────
setMicIndicator(false);
connectEvents();
pollStats();
setInterval(pollStats, 2500);
pushSysLog('SYS: Interface inicializada', 'tool');
