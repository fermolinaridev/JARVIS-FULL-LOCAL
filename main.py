"""
JARVIS — janela "desktop" via Edge em modo app + servidor HTTP local com SSE.

Arquitetura:
  - Servidor HTTP stdlib (ThreadingHTTPServer) serve frontend/ e expõe /api/*
  - SSE (text/event-stream) empurra eventos do backend pro frontend
  - JS chama /api/send_message via fetch
  - Edge é aberto com --app=URL pra parecer um app desktop (sem barra/abas)
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from audio_listener import ClapListener
from brain import Brain
from config import (
    ASSISTANT_NAME,
    CLAP_ENABLED,
    FRONTEND_DIR,
    MODEL_NAME,
    OLLAMA_HOST,
    USER_TITLE,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
)
from memory import Memory
from notes import NotesStore
from tools import get_system_status, list_processes

try:
    import tts_engine  # carrega lazy quando /api/tts é chamado
except Exception as _tts_imp_err:  # noqa: BLE001
    tts_engine = None  # type: ignore[assignment]
    print(f"[JARVIS] TTS local indisponível ({_tts_imp_err}); usando Web Speech.")

MEMORY = Memory()
NOTES = NotesStore()
BRAIN = Brain(MEMORY)


def _greeting() -> str:
    h = datetime.now().hour
    if h < 12:
        period = "Bom dia"
    elif h < 18:
        period = "Boa tarde"
    else:
        period = "Boa noite"
    return f"{period}, {USER_TITLE}. Em que posso lhe ser útil?"


# --- Bus de eventos pra SSE -----------------------------------------------

class EventBus:
    """Distribui eventos pra todos os clientes SSE conectados."""

    def __init__(self) -> None:
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


BUS = EventBus()


_LAST_NET = {"t": 0.0, "rx": 0, "tx": 0}


def _collect_stats() -> dict[str, Any]:
    """Snapshot ao vivo de CPU/RAM/disco/processos pra alimentar o HUD."""
    import psutil  # local pra reduzir latência de import na primeira req

    try:
        sysinfo = get_system_status()
    except Exception as e:
        sysinfo = {"error": str(e)}

    # taxa de rede (KB/s) — diff entre chamadas
    rx_kbps = tx_kbps = 0.0
    try:
        net = psutil.net_io_counters()
        now = time.time()
        last = _LAST_NET.copy()
        if last["t"] > 0:
            dt = max(0.001, now - last["t"])
            rx_kbps = max(0.0, (net.bytes_recv - last["rx"]) / dt / 1024.0)
            tx_kbps = max(0.0, (net.bytes_sent - last["tx"]) / dt / 1024.0)
        _LAST_NET["t"] = now
        _LAST_NET["rx"] = net.bytes_recv
        _LAST_NET["tx"] = net.bytes_sent
    except Exception:
        pass

    # temperaturas: tenta sensores; se não, devolve None
    temps: dict[str, float] = {}
    try:
        sensors = getattr(psutil, "sensors_temperatures", lambda: {})() or {}
        for label, entries in sensors.items():
            for i, e in enumerate(entries[:4]):
                if e.current:
                    temps[f"{label}-{i}"] = round(float(e.current), 1)
    except Exception:
        pass

    # top processos
    try:
        procs = list_processes(top_n=6)
    except Exception:
        procs = []

    return {
        "system": sysinfo,
        "net": {"rx_kbps": round(rx_kbps, 1), "tx_kbps": round(tx_kbps, 1)},
        "temps": temps,
        "processes": [
            {
                "pid": p.get("pid"),
                "name": p.get("name") or "—",
                "cpu": round(float(p.get("cpu_percent") or 0.0), 1),
                "mem": round(float(p.get("memory_percent") or 0.0), 1),
            }
            for p in procs
        ],
        "model": MODEL_NAME,
        "ts": time.time(),
    }


def process_turn(text: str) -> None:
    """Roda em background; despeja eventos no bus."""
    try:
        for ev in BRAIN.think(text):
            BUS.publish(ev)
    except Exception as e:
        BUS.publish({"type": "error", "message": f"falha: {e}"})


# --- Handler HTTP --------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "JARVIS/1.0"

    # Silencia logs ruidosos do http.server
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_text(self, code: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # cliente desconectou — sem drama

    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False)
        self._send_text(code, body, "application/json; charset=utf-8")

    def _serve_static(self, rel: str) -> None:
        # Resolve seguro contra path traversal
        rel = rel.lstrip("/")
        if not rel or rel.endswith("/"):
            rel = (rel + "index.html").lstrip("/")
        target = (FRONTEND_DIR / rel).resolve()
        try:
            target.relative_to(FRONTEND_DIR.resolve())
        except ValueError:
            self._send_text(403, "forbidden")
            return
        if not target.is_file():
            self._send_text(404, "not found")
            return
        ext = target.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
            ".js":   "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".svg":  "image/svg+xml",
            ".ico":  "image/x-icon",
        }.get(ext, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = BUS.subscribe()
        try:
            # Hello inicial pra garantir abertura imediata
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    # heartbeat de keep-alive
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            BUS.unsubscribe(q)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/api/events":
            self._serve_sse()
            return
        if url.path == "/api/health":
            ok, detail = BRAIN.health_check()
            self._send_json(200, {"ok": ok, "message": detail})
            return
        if url.path == "/api/stats":
            self._send_json(200, _collect_stats())
            return
        if url.path == "/api/tts/status":
            self._send_json(
                200,
                tts_engine.ENGINE.status if tts_engine else {"loaded": False, "error": "module not imported"},
            )
            return
        # Estáticos
        rel = url.path if url.path != "/" else "/index.html"
        self._serve_static(rel)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        try:
            text = body.decode("utf-8") if body else ""
        except UnicodeDecodeError:
            # cliente mandou em latin-1/cp1252 (curl bash no Windows, etc.)
            text = body.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {}

        if url.path == "/api/send_message":
            text = (payload.get("text") or "").strip()
            if not text:
                self._send_json(400, {"ok": False, "error": "vazio"})
                return
            threading.Thread(target=process_turn, args=(text,), daemon=True).start()
            self._send_json(200, {"ok": True})
            return

        if url.path == "/api/greet":
            msg = _greeting()
            BUS.publish({"type": "wake", "message": msg})
            self._send_json(200, {"ok": True, "message": msg})
            return

        if url.path == "/api/tts":
            if tts_engine is None:
                self._send_json(503, {"ok": False, "error": "TTS local indisponível"})
                return
            tts_text = (payload.get("text") or "").strip()
            language = (payload.get("language") or "pt").strip() or "pt"
            if not tts_text:
                self._send_json(400, {"ok": False, "error": "texto vazio"})
                return
            try:
                wav = tts_engine.synthesize(tts_text, language=language)
            except Exception as e:  # noqa: BLE001
                self._send_json(503, {"ok": False, "error": str(e)})
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(wav)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
            return

        self._send_text(404, "not found")


def _ollama_alive(timeout: float = 1.5) -> bool:
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=timeout)
        return r.ok
    except Exception:
        return False


def _ollama_executable() -> str | None:
    """Localiza ollama.exe no Windows ou ollama no PATH."""
    exe = shutil.which("ollama")
    if exe:
        return exe
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            rf"{local}\Programs\Ollama\ollama.exe",
            r"C:\Program Files\Ollama\ollama.exe",
            r"C:\Program Files (x86)\Ollama\ollama.exe",
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
    return None


def _ensure_ollama_running(wait_s: float = 12.0) -> bool:
    """Se Ollama não estiver rodando, tenta subir 'ollama serve' em background."""
    if _ollama_alive():
        return True
    exe = _ollama_executable()
    if not exe:
        print(f"[{ASSISTANT_NAME}] Ollama não encontrado no sistema. Instale: https://ollama.com/download")
        return False
    print(f"[{ASSISTANT_NAME}] Ollama offline — subindo 'ollama serve'...")
    try:
        creationflags = 0
        if sys.platform == "win32":
            # CREATE_NO_WINDOW = 0x08000000 — não abre prompt extra no Windows
            creationflags = 0x08000000
        subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    except Exception as e:
        print(f"[{ASSISTANT_NAME}] falha ao iniciar Ollama: {e}")
        return False

    # Espera ficar online
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if _ollama_alive(timeout=1.0):
            print(f"[{ASSISTANT_NAME}] Ollama online.")
            return True
        time.sleep(0.5)
    print(f"[{ASSISTANT_NAME}] Ollama não respondeu em {wait_s}s — seguindo mesmo assim.")
    return False


def _free_port() -> int:
    forced = os.environ.get("JARVIS_PORT")
    if forced:
        return int(forced)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _open_in_app_window(url: str) -> bool:
    """Tenta abrir no Edge/Chrome em modo --app (sem chrome do navegador)."""
    candidates: list[list[str]] = []

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        edge_paths = [
            rf"{program_files}\Microsoft\Edge\Application\msedge.exe",
            rf"{program_files_x86}\Microsoft\Edge\Application\msedge.exe",
        ]
        chrome_paths = [
            rf"{program_files}\Google\Chrome\Application\chrome.exe",
            rf"{program_files_x86}\Google\Chrome\Application\chrome.exe",
            rf"{local}\Google\Chrome\Application\chrome.exe",
        ]
        for p in edge_paths + chrome_paths:
            if os.path.isfile(p):
                candidates.append([p, f"--app={url}", f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}"])
                break
    # Fallback genérico
    for browser in ("msedge", "chrome", "google-chrome", "chromium"):
        which = shutil.which(browser)
        if which:
            candidates.append([which, f"--app={url}", f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}"])
            break

    for cmd in candidates:
        try:
            subprocess.Popen(cmd)
            return True
        except Exception:
            continue
    return False


def main() -> int:
    # Garante que o Ollama está no ar antes de qualquer coisa
    _ensure_ollama_running()

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True

    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="HttpServer")
    server_thread.start()

    url = f"http://127.0.0.1:{port}/"
    print(f"[{ASSISTANT_NAME}] servidor iniciado em {url}")

    clap_listener: ClapListener | None = None
    if CLAP_ENABLED:
        clap_listener = ClapListener(
            on_double_clap=lambda: BUS.publish({"type": "wake", "message": _greeting()})
        )
        if clap_listener.available():
            clap_listener.start()
            print(f"[{ASSISTANT_NAME}] clap detector ativo (bata 2 palmas pra cumprimentar).")
        else:
            print(f"[{ASSISTANT_NAME}] sounddevice indisponível — clap detector desativado.")

    if os.environ.get("JARVIS_NO_BROWSER") == "1":
        print(f"[{ASSISTANT_NAME}] navegador desativado (JARVIS_NO_BROWSER=1).")
    elif not _open_in_app_window(url):
        # Último recurso: abre no navegador padrão
        import webbrowser
        webbrowser.open(url)
        print(f"[{ASSISTANT_NAME}] abriu no navegador padrão (Edge/Chrome em modo app indisponível).")

    print(f"[{ASSISTANT_NAME}] online. Ctrl+C pra encerrar.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n[{ASSISTANT_NAME}] encerrando...")
    finally:
        if clap_listener:
            clap_listener.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
