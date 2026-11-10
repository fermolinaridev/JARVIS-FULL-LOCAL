"""Módulo de Ação: ferramentas que JARVIS pode invocar via tool-calling."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import psutil

DANGEROUS_KEYWORDS = (
    "rm ", "rm -", "del ", "rmdir", "format ", "shutdown", "reboot",
    "mkfs", ":(){", "dd if=", "> /dev/", "diskpart", "fdisk",
    "taskkill", "kill -9",
)


# Memória e notas são injetadas pelo main.py via bind(). Antes as tools faziam
# `from main import MEMORY`: como o programa roda como __main__, isso executava
# o main.py de novo como um 2º módulo (~1 s na 1ª chamada) e criava uma SEGUNDA
# Memory — fatos salvos por `remember` não chegavam ao Brain até reiniciar.
_MEMORY: Any = None
_NOTES: Any = None


def bind(memory: Any, notes: Any) -> None:
    global _MEMORY, _NOTES
    _MEMORY, _NOTES = memory, notes


def _memory():
    if _MEMORY is None:
        raise RuntimeError("tools.bind() não foi chamado")
    return _MEMORY


def _notes():
    if _NOTES is None:
        raise RuntimeError("tools.bind() não foi chamado")
    return _NOTES


# Uso de CPU sem bloquear: média desde a amostra anterior, calculada aqui com
# psutil.cpu_times(). (psutil.cpu_percent(interval=None) guarda o estado POR
# THREAD e o servidor atende cada request numa thread nova — devolvia sempre 0.)
# Antes cada chamada dormia 0,4 s (interval=0.4). O HUD consulta a cada 2,5 s,
# então o valor está sempre fresco; amostras a menos de 0,5 s reusam a anterior.
_CPU_LOCK = threading.Lock()
_CPU: dict[str, float] = {"t": 0.0, "busy": 0.0, "total": 0.0, "value": 0.0}


def _cpu_percent() -> float:
    with _CPU_LOCK:
        now = time.monotonic()
        if now - _CPU["t"] >= 0.5:
            times = psutil.cpu_times()
            total = float(sum(times))
            busy = total - float(times.idle)
            if _CPU["total"] and total > _CPU["total"]:
                _CPU["value"] = round(100.0 * (busy - _CPU["busy"]) / (total - _CPU["total"]), 1)
            _CPU.update(t=now, busy=busy, total=total)
        return _CPU["value"]


_cpu_percent()  # primeira amostra só inicia a contagem


# --- Sistema -----------------------------------------------------------------

def get_system_status() -> dict[str, Any]:
    cpu_percent = _cpu_percent()
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    boot_time = datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.now() - boot_time
    return {
        "cpu_percent": cpu_percent,
        "cpu_count": psutil.cpu_count(),
        "ram_used_gb": round(mem.used / (1024 ** 3), 2),
        "ram_total_gb": round(mem.total / (1024 ** 3), 2),
        "ram_percent": mem.percent,
        "disk_used_gb": round(disk.used / (1024 ** 3), 2),
        "disk_total_gb": round(disk.total / (1024 ** 3), 2),
        "disk_percent": disk.percent,
        "uptime": str(uptime).split(".")[0],
        "platform": platform.system(),
        "platform_release": platform.release(),
    }


def list_processes(top_n: int = 10) -> list[dict[str, Any]]:
    procs: list[dict[str, Any]] = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = p.info
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not info.get("pid"):
            continue  # "System Idle Process" não é um processo de verdade
        # 2 casas bastam; os floats crus (4.818294872900608e-05) só gastam tokens
        info["cpu_percent"] = round(info.get("cpu_percent") or 0.0, 1)
        info["memory_percent"] = round(info.get("memory_percent") or 0.0, 2)
        procs.append(info)
    procs.sort(key=lambda x: x.get("cpu_percent") or 0, reverse=True)
    return procs[:max(1, min(top_n, 50))]


def execute_shell(command: str, timeout: int = 30) -> dict[str, Any]:
    is_dangerous = any(kw in command.lower() for kw in DANGEROUS_KEYWORDS)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        return {
            "command": command,
            "exit_code": result.returncode,
            "stdout": (result.stdout or "")[-4000:],
            "stderr": (result.stderr or "")[-2000:],
            "dangerous": is_dangerous,
        }
    except subprocess.TimeoutExpired:
        return {"command": command, "error": f"timeout após {timeout}s.", "dangerous": is_dangerous}
    except Exception as e:
        return {"command": command, "error": str(e), "dangerous": is_dangerous}


def read_file(path: str, max_chars: int = 8000) -> dict[str, Any]:
    p = Path(path).expanduser()
    if not p.exists():
        return {"path": str(p), "error": "arquivo não encontrado."}
    if not p.is_file():
        return {"path": str(p), "error": "caminho não é um arquivo."}
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        return {
            "path": str(p),
            "size_bytes": p.stat().st_size,
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
        }
    except Exception as e:
        return {"path": str(p), "error": str(e)}


def list_directory(path: str = ".") -> dict[str, Any]:
    p = Path(path).expanduser()
    if not p.exists():
        return {"path": str(p), "error": "caminho não existe."}
    if not p.is_dir():
        return {"path": str(p), "error": "caminho não é um diretório."}
    try:
        entries = []
        for child in sorted(p.iterdir()):
            try:
                entries.append({
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size": child.stat().st_size if child.is_file() else None,
                })
            except OSError:
                continue
        return {"path": str(p.resolve()), "count": len(entries), "entries": entries[:200]}
    except Exception as e:
        return {"path": str(p), "error": str(e)}


def get_current_time() -> dict[str, str]:
    now = datetime.now()
    weekdays = ["segunda-feira","terça-feira","quarta-feira","quinta-feira","sexta-feira","sábado","domingo"]
    return {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": weekdays[now.weekday()],
    }


# --- Memória ----------------------------------------------------------------

def remember(key: str, value: str) -> dict[str, Any]:
    _memory().remember(key, value)
    return {"ok": True, "key": key, "value": value}


def recall(key: str) -> dict[str, Any]:
    value = _memory().recall(key)
    if value is None:
        return {"found": False, "key": key}
    return {"found": True, "key": key, "value": value}


def list_facts() -> dict[str, Any]:
    return {"facts": _memory().all_facts()}


# --- Notas e agenda ---------------------------------------------------------

def add_note(text: str, title: str = "") -> dict[str, Any]:
    return _notes().add_note(text, title)


def list_notes(limit: int = 20) -> dict[str, Any]:
    return {"notes": _notes().list_notes(limit)}


def search_notes(query: str) -> dict[str, Any]:
    return {"matches": _notes().search_notes(query)}


def delete_note(note_id: str) -> dict[str, Any]:
    return {"ok": _notes().delete_note(note_id), "id": note_id}


def add_event(title: str, when: str, description: str = "") -> dict[str, Any]:
    return _notes().add_event(title, when, description)


def list_events(upcoming_only: bool = False) -> dict[str, Any]:
    return {"events": _notes().list_events(upcoming_only)}


def delete_event(event_id: str) -> dict[str, Any]:
    return {"ok": _notes().delete_event(event_id), "id": event_id}


# --- Apps, janelas, automação -----------------------------------------------

# Atalhos comuns no Windows. JARVIS tenta resolver pelo nome também.
WINDOWS_APP_ALIASES = {
    "bloco de notas": "notepad",
    "notepad": "notepad",
    "calculadora": "calc",
    "calc": "calc",
    "explorador": "explorer",
    "explorer": "explorer",
    "navegador": "msedge",
    "edge": "msedge",
    "chrome": "chrome",
    "firefox": "firefox",
    "terminal": "wt",
    "powershell": "powershell",
    "cmd": "cmd",
    "spotify": "spotify",
    "vscode": "code",
    "code": "code",
    "vs code": "code",
    "paint": "mspaint",
    "configurações": "ms-settings:",
    "settings": "ms-settings:",
}


def open_app(name: str) -> dict[str, Any]:
    """Abre um aplicativo pelo nome (alias) ou caminho."""
    key = name.strip().lower()
    target = WINDOWS_APP_ALIASES.get(key, name)
    is_windows = platform.system() == "Windows"
    try:
        if is_windows:
            if target.startswith("ms-settings:") or target.startswith("http"):
                os.startfile(target)
            elif shutil.which(target):
                subprocess.Popen([target], shell=False)
            else:
                # Fallback: deixa o shell encontrar (apps na PATH ou no menu Iniciar)
                subprocess.Popen(["cmd", "/c", "start", "", target], shell=False)
        else:
            subprocess.Popen([target])
        return {"ok": True, "opened": target}
    except Exception as e:
        return {"ok": False, "error": str(e), "target": target}


def list_windows() -> dict[str, Any]:
    """Lista janelas visíveis (título não-vazio)."""
    try:
        import pygetwindow as gw
    except ImportError:
        return {"error": "pygetwindow indisponível."}
    titles = [t for t in gw.getAllTitles() if t and t.strip()]
    return {"count": len(titles), "windows": titles[:60]}


def focus_window(title: str) -> dict[str, Any]:
    """Foca a primeira janela cujo título contém `title` (case-insensitive)."""
    try:
        import pygetwindow as gw
    except ImportError:
        return {"error": "pygetwindow indisponível."}
    matches = [w for w in gw.getAllWindows() if title.lower() in (w.title or "").lower() and w.title.strip()]
    if not matches:
        return {"ok": False, "error": f"nenhuma janela com '{title}' encontrada."}
    win = matches[0]
    try:
        if win.isMinimized:
            win.restore()
        win.activate()
        return {"ok": True, "title": win.title}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def close_window(title: str) -> dict[str, Any]:
    try:
        import pygetwindow as gw
    except ImportError:
        return {"error": "pygetwindow indisponível."}
    matches = [w for w in gw.getAllWindows() if title.lower() in (w.title or "").lower() and w.title.strip()]
    if not matches:
        return {"ok": False, "error": f"nenhuma janela com '{title}' encontrada."}
    try:
        matches[0].close()
        return {"ok": True, "title": matches[0].title}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_active_window() -> dict[str, Any]:
    try:
        import pygetwindow as gw
    except ImportError:
        return {"error": "pygetwindow indisponível."}
    try:
        win = gw.getActiveWindow()
        return {"title": win.title if win else None}
    except Exception as e:
        return {"error": str(e)}


def type_text(text: str, interval: float = 0.02) -> dict[str, Any]:
    """Digita texto na janela ativa."""
    try:
        import pyautogui
    except ImportError:
        return {"error": "pyautogui indisponível."}
    try:
        pyautogui.write(text, interval=interval)
        return {"ok": True, "chars": len(text)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def press_key(key: str) -> dict[str, Any]:
    """Pressiona uma tecla (ex: 'enter', 'esc', 'win', 'space')."""
    try:
        import pyautogui
    except ImportError:
        return {"error": "pyautogui indisponível."}
    try:
        pyautogui.press(key)
        return {"ok": True, "key": key}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def hotkey(*keys: str) -> dict[str, Any]:
    """Pressiona combinação de teclas (ex: hotkey('ctrl','c'))."""
    try:
        import pyautogui
    except ImportError:
        return {"error": "pyautogui indisponível."}
    try:
        pyautogui.hotkey(*keys)
        return {"ok": True, "keys": list(keys)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def clipboard_get() -> dict[str, Any]:
    try:
        import pyperclip
        return {"text": pyperclip.paste()}
    except Exception as e:
        return {"error": str(e)}


def clipboard_set(text: str) -> dict[str, Any]:
    try:
        import pyperclip
        pyperclip.copy(text)
        return {"ok": True, "chars": len(text)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def screenshot(path: str = "") -> dict[str, Any]:
    """Captura a tela. Salva em `path` (ou em ./screenshot_<ts>.png)."""
    try:
        import pyautogui
    except ImportError:
        return {"error": "pyautogui indisponível."}
    if not path:
        path = f"screenshot_{int(time.time())}.png"
    try:
        img = pyautogui.screenshot()
        img.save(path)
        return {"ok": True, "path": str(Path(path).resolve())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- Web (URLs, YouTube, Spotify) -------------------------------------------

import webbrowser
import urllib.parse


def open_url(url: str) -> dict[str, Any]:
    """Abre uma URL no navegador padrão. Aceita 'youtube.com' sem protocolo."""
    if not url:
        return {"ok": False, "error": "url vazia"}
    u = url.strip()
    if not u.startswith(("http://", "https://", "spotify:", "mailto:", "ftp:")):
        u = "https://" + u
    try:
        webbrowser.open(u, new=2)
        return {"ok": True, "url": u}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def youtube(query: str = "") -> dict[str, Any]:
    """Abre o YouTube. Se `query` for dado, vai direto pra busca; senão abre a home."""
    if query:
        q = urllib.parse.quote_plus(query)
        url = f"https://www.youtube.com/results?search_query={q}"
    else:
        url = "https://www.youtube.com/"
    return open_url(url)


def spotify(query: str = "") -> dict[str, Any]:
    """Abre o Spotify (app desktop via URI; web player se não houver).
    Se `query` for dado, faz busca; senão abre a home."""
    # 1) Tenta o app desktop via protocolo spotify:
    try:
        if query:
            os.startfile(f"spotify:search:{query}")
        else:
            os.startfile("spotify:")
        return {"ok": True, "via": "app", "query": query or None}
    except (OSError, FileNotFoundError, AttributeError):
        pass
    # 2) Fallback: web player
    if query:
        q = urllib.parse.quote_plus(query)
        return open_url(f"https://open.spotify.com/search/{q}")
    return open_url("https://open.spotify.com/")


# --- Volume e mídia ---------------------------------------------------------

# VK codes do Windows pra teclas de mídia. Funcionam em qualquer player ativo.
_VK = {
    "vol_up":   0xAF,
    "vol_down": 0xAE,
    "vol_mute": 0xAD,
    "play":     0xB3,  # VK_MEDIA_PLAY_PAUSE
    "next":     0xB0,
    "prev":     0xB1,
    "stop":     0xB2,
}
_KEYEVENTF_KEYUP = 0x0002


def _press_media(key: str, presses: int = 1) -> bool:
    """Tenta enviar via Win32 API (mais confiável); fallback pyautogui."""
    if platform.system() == "Windows":
        try:
            import ctypes
            vk = _VK.get(key)
            if vk is None:
                return False
            for _ in range(max(1, int(presses))):
                ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
                ctypes.windll.user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
                time.sleep(0.03)
            return True
        except Exception:
            pass
    # Fallback: pyautogui
    try:
        import pyautogui
        alias = {
            "vol_up": "volumeup", "vol_down": "volumedown", "vol_mute": "volumemute",
            "play": "playpause", "next": "nexttrack", "prev": "prevtrack", "stop": "stop",
        }.get(key)
        if not alias:
            return False
        pyautogui.press(alias, presses=max(1, int(presses)))
        return True
    except Exception:
        return False


def volume_up(steps: int = 4) -> dict[str, Any]:
    """Aumenta o volume do sistema. Cada step ≈ 2%."""
    steps = max(1, min(int(steps), 25))
    ok = _press_media("vol_up", steps)
    return {"ok": ok, "steps": steps}


def volume_down(steps: int = 4) -> dict[str, Any]:
    """Diminui o volume do sistema. Cada step ≈ 2%."""
    steps = max(1, min(int(steps), 25))
    ok = _press_media("vol_down", steps)
    return {"ok": ok, "steps": steps}


def volume_mute() -> dict[str, Any]:
    """Alterna mudo do sistema."""
    ok = _press_media("vol_mute", 1)
    return {"ok": ok}


def volume_set(percent: int) -> dict[str, Any]:
    """Define volume absoluto (0-100). Usa pycaw se disponível; senão aproxima por steps."""
    percent = max(0, min(int(percent), 100))
    # 1) Tenta pycaw (preciso)
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(interface, POINTER(IAudioEndpointVolume))
        vol.SetMasterVolumeLevelScalar(percent / 100.0, None)
        return {"ok": True, "percent": percent, "via": "pycaw"}
    except Exception:
        pass
    # 2) Fallback aproximado: zera e sobe N steps (~2% cada)
    _press_media("vol_down", 50)  # zera
    _press_media("vol_up", percent // 2)
    return {"ok": True, "percent_approx": percent, "via": "steps"}


def media_play_pause() -> dict[str, Any]:
    """Play/pause do player ativo (Spotify, YouTube, mídia do Windows)."""
    return {"ok": _press_media("play", 1)}


def media_next() -> dict[str, Any]:
    """Próxima faixa do player ativo."""
    return {"ok": _press_media("next", 1)}


def media_prev() -> dict[str, Any]:
    """Faixa anterior do player ativo."""
    return {"ok": _press_media("prev", 1)}


def media_stop() -> dict[str, Any]:
    """Para a reprodução."""
    return {"ok": _press_media("stop", 1)}


# --- Schemas para tool-calling ----------------------------------------------

def _fn(name: str, desc: str, props: dict | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props or {},
                "required": required or [],
            },
        },
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn("get_system_status", "Status do sistema: CPU, RAM, disco, uptime."),
    _fn("list_processes", "Processos mais ativos por CPU.",
        {"top_n": {"type": "integer", "default": 10}}),
    _fn("execute_shell", "Executa um comando shell. Comandos destrutivos ficam marcados.",
        {"command": {"type": "string"}, "timeout": {"type": "integer", "default": 30}}, ["command"]),
    _fn("read_file", "Lê o conteúdo (texto) de um arquivo.",
        {"path": {"type": "string"}}, ["path"]),
    _fn("list_directory", "Lista o conteúdo de um diretório.",
        {"path": {"type": "string", "default": "."}}),
    _fn("get_current_time", "Data e hora atuais."),
    _fn("remember", "Armazena um fato persistente sobre o usuário.",
        {"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]),
    _fn("recall", "Recupera um fato pela chave.",
        {"key": {"type": "string"}}, ["key"]),
    _fn("list_facts", "Lista todos os fatos lembrados."),

    _fn("add_note", "Cria uma nota livre na agenda do usuário.",
        {"text": {"type": "string"}, "title": {"type": "string"}}, ["text"]),
    _fn("list_notes", "Lista as notas mais recentes.",
        {"limit": {"type": "integer", "default": 20}}),
    _fn("search_notes", "Busca notas por termo.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("delete_note", "Apaga uma nota pelo id.",
        {"note_id": {"type": "string"}}, ["note_id"]),
    _fn("add_event", "Adiciona um compromisso à agenda. `when` em ISO (YYYY-MM-DDTHH:MM).",
        {"title": {"type": "string"}, "when": {"type": "string"}, "description": {"type": "string"}}, ["title", "when"]),
    _fn("list_events", "Lista compromissos.",
        {"upcoming_only": {"type": "boolean", "default": False}}),
    _fn("delete_event", "Apaga um compromisso pelo id.",
        {"event_id": {"type": "string"}}, ["event_id"]),

    _fn("open_app", "Abre um aplicativo pelo nome (notepad, calc, chrome, spotify, code, etc.) ou caminho.",
        {"name": {"type": "string"}}, ["name"]),
    _fn("list_windows", "Lista janelas abertas (títulos)."),
    _fn("focus_window", "Foca a primeira janela cujo título contenha o termo.",
        {"title": {"type": "string"}}, ["title"]),
    _fn("close_window", "Fecha a primeira janela cujo título contenha o termo.",
        {"title": {"type": "string"}}, ["title"]),
    _fn("get_active_window", "Retorna o título da janela em foco."),

    _fn("type_text", "Digita texto na janela ativa.",
        {"text": {"type": "string"}, "interval": {"type": "number", "default": 0.02}}, ["text"]),
    _fn("press_key", "Pressiona uma tecla (enter, esc, space, win, f5, ...).",
        {"key": {"type": "string"}}, ["key"]),
    _fn("hotkey", "Combinação de teclas. Passe a lista em `keys`, ex: ['ctrl','c'].",
        {"keys": {"type": "array", "items": {"type": "string"}}}, ["keys"]),
    _fn("clipboard_get", "Lê o texto atual do clipboard."),
    _fn("clipboard_set", "Define o texto do clipboard.",
        {"text": {"type": "string"}}, ["text"]),
    _fn("screenshot", "Captura a tela atual.",
        {"path": {"type": "string", "default": ""}}),

    # Web
    _fn("open_url", "Abre uma URL no navegador padrão (aceita 'youtube.com' sem protocolo).",
        {"url": {"type": "string"}}, ["url"]),
    _fn("youtube", "Abre o YouTube. Use `query` pra buscar um vídeo (ex: 'AC/DC Thunderstruck'). Sem query, abre a home.",
        {"query": {"type": "string"}}),
    _fn("spotify", "Abre o Spotify. Use `query` pra buscar uma música, álbum ou artista (ex: 'AC/DC'). Sem query, abre o app.",
        {"query": {"type": "string"}}),

    # Volume e mídia
    _fn("volume_up", "Aumenta o volume do sistema. Cada step ≈ 2%. Padrão: 4 steps.",
        {"steps": {"type": "integer", "default": 4}}),
    _fn("volume_down", "Diminui o volume do sistema. Cada step ≈ 2%. Padrão: 4 steps.",
        {"steps": {"type": "integer", "default": 4}}),
    _fn("volume_mute", "Alterna mudo do sistema."),
    _fn("volume_set", "Define o volume absoluto, de 0 a 100.",
        {"percent": {"type": "integer"}}, ["percent"]),
    _fn("media_play_pause", "Play/pause da mídia ativa (Spotify, YouTube, qualquer player)."),
    _fn("media_next", "Próxima faixa da mídia ativa."),
    _fn("media_prev", "Faixa anterior da mídia ativa."),
    _fn("media_stop", "Para a reprodução da mídia ativa."),
]


def _hotkey_dispatch(keys=None, **rest):
    if keys is None:
        keys = list(rest.values())
    return hotkey(*keys)


TOOL_REGISTRY: dict[str, Callable[..., Any]] = {
    "get_system_status": get_system_status,
    "list_processes": list_processes,
    "execute_shell": execute_shell,
    "read_file": read_file,
    "list_directory": list_directory,
    "get_current_time": get_current_time,
    "remember": remember,
    "recall": recall,
    "list_facts": list_facts,
    "add_note": add_note,
    "list_notes": list_notes,
    "search_notes": search_notes,
    "delete_note": delete_note,
    "add_event": add_event,
    "list_events": list_events,
    "delete_event": delete_event,
    "open_app": open_app,
    "list_windows": list_windows,
    "focus_window": focus_window,
    "close_window": close_window,
    "get_active_window": get_active_window,
    "type_text": type_text,
    "press_key": press_key,
    "hotkey": _hotkey_dispatch,
    "clipboard_get": clipboard_get,
    "clipboard_set": clipboard_set,
    "screenshot": screenshot,
    # Web
    "open_url": open_url,
    "youtube": youtube,
    "spotify": spotify,
    # Volume e mídia
    "volume_up": volume_up,
    "volume_down": volume_down,
    "volume_mute": volume_mute,
    "volume_set": volume_set,
    "media_play_pause": media_play_pause,
    "media_next": media_next,
    "media_prev": media_prev,
    "media_stop": media_stop,
}


# Sinônimos de parâmetros — modelos pequenos (qwen3.5:4b) às vezes inventam.
# Mapeia 'nome inventado pelo modelo' -> 'nome real do parâmetro'.
PARAM_ALIASES: dict[str, list[str]] = {
    "name":     ["app", "application", "program", "alias", "target", "appname"],
    "title":    ["window", "window_title", "windowname", "name"],
    "path":     ["file", "filename", "filepath", "file_path"],
    "text":     ["message", "string", "content", "input"],
    "term":     ["query", "search", "keyword", "q"],
    "key":      ["keyname", "button"],
    "keys":     ["combo", "combination", "shortcut"],
    "command":  ["cmd", "shell", "exec"],
    "fact":     ["info", "value", "memory"],
    "title_substring": ["title", "name", "substring"],
    "id":       ["uuid", "note_id", "event_id"],
    "when":     ["date", "time", "datetime", "data"],
    "what":     ["title", "subject", "event"],
}


def _normalize_args(fn, args: dict[str, Any]) -> dict[str, Any]:
    """Reescreve args com nomes sinônimos pra os nomes reais do parâmetro,
    e descarta kwargs desconhecidos pra não quebrar."""
    import inspect
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return args or {}
    params = sig.parameters
    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    valid_names = {n for n, p in params.items() if p.kind != inspect.Parameter.VAR_KEYWORD}

    out: dict[str, Any] = {}
    leftovers: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if k in valid_names:
            out[k] = v
            continue
        # Tenta achar o parâmetro real cujo apelido bate com k
        remapped = None
        for real, aliases in PARAM_ALIASES.items():
            if real in valid_names and k in aliases:
                remapped = real
                break
        if remapped and remapped not in out:
            out[remapped] = v
        else:
            leftovers[k] = v

    # Se sobrou exatamente 1 valor leftover e exatamente 1 parâmetro obrigatório
    # ainda não preenchido, joga ele lá (último recurso)
    required_missing = [
        n for n, p in params.items()
        if n not in out
        and p.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
        and p.default is inspect.Parameter.empty
    ]
    if len(leftovers) == 1 and len(required_missing) == 1:
        out[required_missing[0]] = next(iter(leftovers.values()))
    elif accepts_kwargs:
        out.update(leftovers)
    # caso contrário, descarta silenciosamente os leftovers (vale mais executar
    # com defaults do que quebrar a turn inteira)

    return out


def call_tool(name: str, args: dict[str, Any]) -> str:
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return json.dumps({"error": f"ferramenta desconhecida: {name}"}, ensure_ascii=False)
    norm_args = _normalize_args(fn, args or {})
    try:
        result = fn(**norm_args)
    except TypeError as e:
        return json.dumps(
            {"error": f"argumentos inválidos: {e}", "received": list((args or {}).keys()), "normalized": list(norm_args.keys())},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False, default=str)
