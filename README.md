# JARVIS-FULL-LOCAL

Assistente pessoal estilo **J.A.R.V.I.S.** rodando 100% localmente — Python + [Ollama](https://ollama.com), com:

- **HUD estilo Iron Man** (CSS puro): arco-reator central com anéis concêntricos, painéis laterais com stats ao vivo (CPU/RAM/disco/rede/temperatura/processos), radar, log de eventos
- **Voz JARVIS clonada localmente** via **Coqui XTTS-v2** — fala em PT-BR com timbre clonado do Paul Bettany (com fallback pra Web Speech API se a TTS local não estiver disponível)
- **STT**: ouve você via Web Speech API com auto-restart
- **Detecção de palmas**: 2 palmas em sequência → JARVIS te cumprimenta com base no horário
- **Tool calling nativo do Ollama** — JARVIS decide sozinho quando usar cada ferramenta
- **Controle de aplicativos**: abrir/fechar/focar janelas, digitar texto, atalhos de teclado, clipboard, screenshot
- **Memória persistente** + **agenda** (notas e compromissos)
- **GPU**: o Ollama detecta sua placa NVIDIA automaticamente; o XTTS-v2 usa CUDA pra inferência ~realtime

## Pré-requisitos

- Windows 10/11 com WebView2 (já vem no Win11)
- Python 3.10+
- [Ollama](https://ollama.com/download) instalado e rodando
- Modelo com tool-calling (default: `qwen3.5:4b`)
- Microfone (opcional, para voz e palmas)

## Instalação

```bash
# 1. Baixar um modelo com suporte a tool-calling
ollama pull qwen3.5:4b

# 2. Iniciar o servidor Ollama (em outro terminal, se necessário)
ollama serve

# 3. Instalar dependências Python (base)
pip install -r requirements.txt

# 4. (opcional) Voz local do JARVIS estilo filme — instale PyTorch+CUDA e Coqui:
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
pip install coqui-tts

# 5. (opcional) Coloque uma amostra .wav de voz em samples/jarvis_voice.wav
#    Veja samples/README.md pra detalhes (Paul Bettany, ~10s, mono 22050 Hz).
#    Se faltar, o frontend cai pra Web Speech API automaticamente.

# 6. Rodar JARVIS
python main.py
```

## Uso

A janela abre com o orb pulsante no centro. Você pode:

- **Digitar** no campo de input → Enter
- **Clicar no microfone** 🎤 → fala em PT-BR
- **Bater 2 palmas** rápidas → JARVIS te cumprimenta de acordo com o horário (Bom dia / Boa tarde / Boa noite, Senhor)

JARVIS decide sozinho quando usar ferramentas. Exemplos do que dizer:

- *"Como está a CPU?"* → consulta o status do sistema
- *"Abra o bloco de notas"* → executa `open_app`
- *"Anote que preciso comprar leite"* → cria uma nota
- *"Agende uma reunião amanhã às 15h"* → cria evento
- *"Lembre que minha cor favorita é azul"* → fato persistente
- *"Tira um print da tela"* → screenshot
- *"Quais janelas estão abertas?"* → lista janelas
- *"Mude para o Chrome"* → focus_window

## Estados do orb

- **Idle** (cyan suave): aguardando
- **Listening** (cyan brilhante): ouvindo o microfone
- **Thinking** (violeta): processando ou executando ferramenta
- **Speaking** (cyan claro): respondendo (com pulso reativo ao áudio)
- **Error** (vermelho): falhou

## Configuração

Variáveis de ambiente (todas opcionais):

| Variável | Padrão | Descrição |
|---|---|---|
| `JARVIS_MODEL` | `qwen3.5:4b` | Modelo Ollama |
| `OLLAMA_HOST` | `http://localhost:11434` | Endereço do Ollama |
| `JARVIS_USER_TITLE` | `Senhor` | Como JARVIS te chama |
| `JARVIS_NUM_CTX` | `2048` | Janela de contexto (tokens) |
| `JARVIS_WIDTH` / `JARVIS_HEIGHT` | `1100` / `780` | Tamanho da janela |
| `JARVIS_FULLSCREEN` | `0` | `1` para tela cheia |
| `JARVIS_CLAP` | `1` | `0` desativa o detector de palmas |
| `JARVIS_CLAP_THRESHOLD` | `0.35` | Sensibilidade (0.1–0.6, menor = mais sensível) |
| `JARVIS_TTS_SPEAKER` | `Damien Black` | Speaker built-in do XTTS-v2 quando não há sample (`Viktor Eka`, `Royston Min`, `Damien Black`, etc.) |

## Estrutura

```
JARVIS-FULL-LOCAL/
├── main.py              # janela desktop + bridge JS↔Python
├── brain.py             # integração Ollama + tool-calling
├── tools.py             # 26 ferramentas (sistema, apps, notas, automação)
├── memory.py            # memória de curto e longo prazo
├── notes.py             # notas e agenda persistidas
├── audio_listener.py    # detector de palmas (sounddevice + numpy)
├── config.py            # configurações e prompt de personalidade
├── requirements.txt
└── frontend/
    ├── index.html       # UI principal
    ├── style.css        # tema cyan/blue
    ├── orb.js           # orb 3D Three.js
    ├── voice.js         # STT/TTS (Web Speech API)
    └── app.js           # controlador, ponte com Python
```

## Notas de segurança

- Comandos shell potencialmente destrutivos (`rm`, `del`, `format`, `shutdown`, …) são marcados como `dangerous` no resultado e o prompt instrui JARVIS a confirmar antes.
- JARVIS executa com as suas permissões — não rode com privilégios elevados sem necessidade.
- Tudo é local exceto o **reconhecimento de voz** (Web Speech API): no Edge/WebView2, o STT pode usar serviço online da Microsoft. Se quiser STT 100% offline, podemos integrar Whisper local depois.
