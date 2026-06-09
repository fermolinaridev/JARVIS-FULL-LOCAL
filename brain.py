"""Cérebro: integração com Ollama, gerenciamento da conversa e tool-calling.

Inclui um *intent matcher* determinístico — comandos comuns de voz
(volume, mídia, abrir apps/youtube/spotify) são detectados por regex e executados
sem passar pelo LLM. Isso garante latência baixa e 0% de "alucinação de ação"
em modelos pequenos como qwen3:8b.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator

import ollama
import requests

from config import MAX_TOOL_ITERATIONS, MODEL_NAME, NUM_CTX, OLLAMA_HOST, SYSTEM_PROMPT, USER_TITLE
from memory import Memory
from tools import TOOL_SCHEMAS, call_tool


def _to_dict(obj: Any) -> Any:
    """Normaliza respostas do cliente Ollama (pydantic ou dict) para dict puro."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return obj


class Brain:
    def __init__(self, memory: Memory, model: str = MODEL_NAME) -> None:
        self.memory = memory
        self.model = model
        self.client = ollama.Client(host=OLLAMA_HOST)

    def health_check(self) -> tuple[bool, str]:
        """Verifica se Ollama está rodando e o modelo configurado existe."""
        try:
            r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=3)
            r.raise_for_status()
            models = r.json().get("models", [])
            names = [m.get("name", "") for m in models]
            if not any(self.model == n or n.startswith(self.model + ":") for n in names):
                available = ", ".join(names) if names else "nenhum"
                return (
                    False,
                    f"modelo '{self.model}' indisponível (instale com: ollama pull {self.model}). Encontrados: {available}",
                )
            return (True, f"online — modelo '{self.model}' pronto")
        except requests.exceptions.ConnectionError:
            return (False, "Ollama offline. Inicie com: ollama serve")
        except requests.exceptions.Timeout:
            return (False, "Ollama não respondeu (timeout)")
        except Exception as e:
            return (False, f"erro: {e}")

    def _stream_chat(self, messages: list[dict]) -> Iterator[dict[str, Any]]:
        """Wrapper que abstrai streaming do Ollama. Yielda chunks com 'message'."""
        try:
            stream = self.client.chat(
                model=self.model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                # Temperatura baixa: modelos pequenos viram preguiçosos com tool-calling
                # quando temp é alta (alucinam ação em vez de chamar a função).
                options={
                    "num_ctx": NUM_CTX,
                    "temperature": 0.5,
                    "top_p": 0.9,
                },
                stream=True,
            )
        except Exception as e:
            raise RuntimeError(f"falha ao consultar Ollama: {e}") from e
        for chunk in stream:
            yield _to_dict(chunk)

    # --- Intent shortcuts: comandos comuns que NÃO passam pelo LLM ---
    # Cada item é (regex, função-handler). O handler recebe o match e é um
    # gerador que yielda os mesmos eventos do think() normal.

    def _intent_shortcut(self, user_input: str) -> Iterator[dict[str, Any]] | None:
        text = user_input.strip().lower()
        # Limpa pontuação e remove acentos pra match robusto
        norm = re.sub(r"[!?.,]+$", "", text)
        norm = norm.replace("á", "a").replace("ã", "a").replace("â", "a") \
                   .replace("é", "e").replace("ê", "e") \
                   .replace("í", "i") \
                   .replace("ó", "o").replace("ô", "o").replace("õ", "o") \
                   .replace("ú", "u").replace("ç", "c")

        # ---- VOLUME ----
        if re.search(r"\b(aumenta|sobe|levanta|sube|incrementa)\b.*\bvolume\b|\bmais alto\b|\bvolume.*(mais alto|aumenta|sobe)", norm):
            steps = 8 if re.search(r"\b(muito|bem)\b", norm) else 4
            return self._exec_intent("volume_up", {"steps": steps},
                                     f"Volume aumentado, {USER_TITLE}.")
        if re.search(r"\b(diminui|abaixa|baixa|reduz|menor)\b.*\bvolume\b|\bmais baixo\b|\bvolume.*(mais baixo|diminui|abaixa)", norm):
            steps = 8 if re.search(r"\b(muito|bem)\b", norm) else 4
            return self._exec_intent("volume_down", {"steps": steps},
                                     f"Volume reduzido, {USER_TITLE}.")
        if re.search(r"\b(muta|mute|mudo|silencia|sem som|tira o som|desliga o som)\b", norm):
            return self._exec_intent("volume_mute", {},
                                     f"Som silenciado, {USER_TITLE}.")
        # "volume 50" / "coloca o volume em 30"
        m = re.search(r"\bvolume\b.*?(\d{1,3})\b|\b(\d{1,3})\s*por\s*cento\b", norm)
        if m:
            pct = int(m.group(1) or m.group(2))
            if 0 <= pct <= 100:
                return self._exec_intent("volume_set", {"percent": pct},
                                         f"Volume em {pct}%, {USER_TITLE}.")

        # ---- MÍDIA ----
        if re.search(r"\b(pausa|pause|para a musica|para a m[uú]sica|pausar)\b", norm):
            return self._exec_intent("media_play_pause", {},
                                     f"Pausado, {USER_TITLE}.")
        if re.search(r"\b(continua|continuar|retoma|despausa|play)\b(?!\s+lista|\s+book)", norm) and not re.search(r"toca\w*\s+\w", norm):
            return self._exec_intent("media_play_pause", {},
                                     f"Continuando, {USER_TITLE}.")
        if re.search(r"\b(proxima|pr[óo]xima|pula|skip|avanca|avan[çc]a)\b.*\b(musica|m[uú]sica|faixa|track|video|v[íi]deo)?", norm):
            return self._exec_intent("media_next", {},
                                     f"Próxima faixa, {USER_TITLE}.")
        if re.search(r"\b(anterior|volta a musica|volta a m[uú]sica|previous|track anterior)\b", norm):
            return self._exec_intent("media_prev", {},
                                     f"Faixa anterior, {USER_TITLE}.")

        # ---- SPOTIFY ----
        # "tocar X no spotify" / "toca X no spotify" / "spotify X"
        m = re.search(r"\b(?:toca\w*|coloca\w*|p[oõ]e)\s+(?:aquela\s+(?:do|de|da)\s+|musica\s+(?:do|de|da)\s+|m[uú]sica\s+(?:do|de|da)\s+)?(.+?)\s+(?:no|na|pelo|pela)\s+spotify\b", norm)
        if m:
            query = m.group(1).strip()
            return self._exec_spotify_play(query)
        m = re.search(r"\bspotify\b.*?(?:tocar|toca|coloca|p[oõ]e)\s+(.+)$", norm)
        if m:
            return self._exec_spotify_play(m.group(1).strip())
        # "abrir spotify" / "abre o spotify"
        if re.search(r"\babr[ei]r?\b.*\bspotify\b|^\s*spotify\s*$", norm):
            return self._exec_intent("spotify", {},
                                     f"Spotify aberto, {USER_TITLE}.")

        # ---- YOUTUBE ----
        # "procura X no youtube" / "youtube X"
        m = re.search(r"\b(?:procura|pesquisa|busca|toca|coloca)\s+(.+?)\s+no\s+youtube\b", norm)
        if m:
            return self._exec_intent("youtube", {"query": m.group(1).strip()},
                                     f"Buscando '{m.group(1).strip()}' no YouTube, {USER_TITLE}.")
        m = re.search(r"\byoutube\b\s+(.+)$", norm)
        if m and not re.search(r"^\s*(abr[ei]r?|abrir)", norm):
            return self._exec_intent("youtube", {"query": m.group(1).strip()},
                                     f"Buscando '{m.group(1).strip()}' no YouTube, {USER_TITLE}.")
        if re.search(r"\babr[ei]r?\b.*\byoutube\b|^\s*youtube\s*$", norm):
            return self._exec_intent("youtube", {},
                                     f"YouTube aberto, {USER_TITLE}.")

        # ---- APPS ----
        # "abre o notepad" / "abrir bloco de notas" / "abre calculadora"
        m = re.match(r"^\s*(?:abr[ei]r?|abrir|inicia|iniciar|liga|ligar)\s+(?:o|a|os|as)?\s*(.+)$", norm)
        if m:
            target = m.group(1).strip()
            # Pula casos que já tratamos acima
            if not re.search(r"\b(youtube|spotify|volume|som|musica|m[uú]sica)\b", target):
                return self._exec_intent("open_app", {"name": target},
                                         f"Abrindo {target}, {USER_TITLE}.")

        return None  # nenhum match → cai no LLM normal

    def _exec_intent(self, tool_name: str, args: dict, confirm_msg: str) -> Iterator[dict]:
        """Executa uma tool diretamente e emite os eventos esperados."""
        yield {"type": "tool_call", "name": tool_name, "args": args}
        result = call_tool(tool_name, args)
        yield {"type": "tool_result", "name": tool_name, "result": result}
        yield {"type": "assistant_start"}
        for ch in confirm_msg:
            yield {"type": "token", "text": ch}
        yield {"type": "assistant_end", "full_text": confirm_msg}
        self.memory.add_message("assistant", confirm_msg)

    def _exec_spotify_play(self, query: str) -> Iterator[dict]:
        """Spotify search + auto-play: abre busca, espera carregar, dá play."""
        # 1) Abre busca no spotify
        yield {"type": "tool_call", "name": "spotify", "args": {"query": query}}
        result = call_tool("spotify", {"query": query})
        yield {"type": "tool_result", "name": "spotify", "result": result}
        # 2) Aguarda 2s pro app carregar e tenta dar play (best-effort)
        time.sleep(2.0)
        yield {"type": "tool_call", "name": "media_play_pause", "args": {}}
        result2 = call_tool("media_play_pause", {})
        yield {"type": "tool_result", "name": "media_play_pause", "result": result2}
        msg = f"Tocando '{query}' no Spotify, {USER_TITLE}."
        yield {"type": "assistant_start"}
        for ch in msg:
            yield {"type": "token", "text": ch}
        yield {"type": "assistant_end", "full_text": msg}
        self.memory.add_message("assistant", msg)

    def think(self, user_input: str) -> Iterator[dict[str, Any]]:
        """
        Processa uma interação com streaming. Gera eventos:
          {"type": "tool_call", "name", "args"}
          {"type": "tool_result", "name", "result"}
          {"type": "assistant_start"}
          {"type": "token", "text"}
          {"type": "assistant_end", "full_text"}
          {"type": "error", "message"}
        """
        # 1) Tenta intent shortcut (comandos de voz comuns) — bypassa o LLM
        shortcut = self._intent_shortcut(user_input)
        if shortcut is not None:
            self.memory.add_message("user", user_input)
            yield from shortcut
            return

        # 2) Caso contrário, fluxo normal com o LLM
        self.memory.add_message("user", user_input)

        for _ in range(MAX_TOOL_ITERATIONS):
            full_content = ""
            tool_calls: list[Any] = []
            started = False

            try:
                for chunk in self._stream_chat(self.memory.messages_for_model(SYSTEM_PROMPT)):
                    msg = chunk.get("message") or {}
                    piece = msg.get("content") or ""
                    if piece:
                        if not started:
                            yield {"type": "assistant_start"}
                            started = True
                        full_content += piece
                        yield {"type": "token", "text": piece}
                    # tool_calls pode vir incrementalmente ou só no final
                    tcs = msg.get("tool_calls") or []
                    if tcs:
                        tool_calls = tcs  # último wins (Ollama costuma mandar no chunk final)
            except RuntimeError as e:
                yield {"type": "error", "message": str(e)}
                return
            except Exception as e:
                yield {"type": "error", "message": f"erro inesperado: {e}"}
                return

            if not tool_calls:
                # Resposta final do modelo
                self.memory.add_message("assistant", full_content)
                if not started:
                    # Nada foi gerado (modelo retornou vazio sem tool) — emite stub
                    yield {"type": "assistant_start"}
                    fallback = "[Senhor, não consegui formular uma resposta. Pode reformular?]"
                    for ch in fallback:
                        yield {"type": "token", "text": ch}
                    yield {"type": "assistant_end", "full_text": fallback}
                else:
                    yield {"type": "assistant_end", "full_text": full_content}
                return

            # Ollama gerou tool_calls — registra na memória e executa
            self.memory.add_message(
                "assistant",
                full_content,
                tool_calls=tool_calls,
            )

            for tc in tool_calls:
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                yield {"type": "tool_call", "name": name, "args": args}
                result = call_tool(name, args)
                yield {"type": "tool_result", "name": name, "result": result}
                self.memory.add_message("tool", result, name=name)

        warning = "[Limite de iterações de ferramentas atingido.]"
        yield {"type": "assistant_start"}
        for ch in warning:
            yield {"type": "token", "text": ch}
        yield {"type": "assistant_end", "full_text": warning}
