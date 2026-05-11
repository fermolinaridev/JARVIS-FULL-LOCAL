# Voz do JARVIS — amostra de referência

Coloque **um arquivo de áudio** aqui pra o XTTS-v2 clonar a voz do JARVIS.

## O que colocar

- **Nome canônico:** `jarvis_voice.wav`
- **Duração:** 6–15 segundos (mínimo 6s; máx ~30s é o sweet spot do XTTS-v2)
- **Formato:** WAV mono 22050 Hz ou 24000 Hz, 16-bit PCM (o XTTS converte
  internamente, mas WAV limpo dá melhor resultado)
- **Conteúdo:** voz **falando claramente** (sem música/SFX por baixo, sem
  reverb pesado). Frases neutras funcionam melhor que gritos ou sussurros.

## Onde achar

A voz do JARVIS no MCU é do **Paul Bettany** (também é o Visão). Você pode
extrair um trecho limpo de qualquer cena onde o JARVIS aparece falando
sozinho — por exemplo:

- *Iron Man 1* (2008) — várias cenas no laboratório de Stark.
- *Iron Man 2* (2010) — diálogos no laboratório e nas armaduras.
- *The Avengers* (2012) — operações na Torre Stark.

Use uma ferramenta tipo Audacity pra:
1. Cortar 10s de fala limpa.
2. Remover trilha de fundo (Effect → Noise Reduction, ou separação de stems
   com o Spleeter/Demucs se tiver muita música).
3. Exportar como `jarvis_voice.wav` (mono, 22050 Hz, 16-bit PCM) **aqui
   neste diretório**.

## Verificação

Se o XTTS-v2 não encontrar `jarvis_voice.wav`, ele pega o primeiro `.wav`
em ordem alfabética dentro deste diretório.

Se não houver **nenhum** `.wav`, o engine cai num **speaker built-in** do
próprio XTTS-v2 (default `Damien Black` — masculino grave). Você pode
trocar pra outra voz built-in setando `JARVIS_TTS_SPEAKER` antes de
iniciar:

```powershell
$env:JARVIS_TTS_SPEAKER = "Viktor Eka"; python main.py
```

Vozes masculinas disponíveis: `Damien Black`, `Viktor Eka`, `Royston Min`,
`Baldur Sanjin`, `Craig Gutsy`, `Andrew Chipper`, `Ludvig Milivoj`,
`Abrahan Mack`, `Adde Michal`, `Badr Odhiambo`, `Dionisio Schuyler`,
`Gilberto Mathias`, `Ilkin Urbano`, `Kazuhiko Atallah`, `Suad Qasim`.

Mesmo no modo built-in o frontend recebe áudio do XTTS-v2 (não cai mais
pra Web Speech). O fallback pra Web Speech só rola se o módulo `tts_engine`
não conseguir nem carregar (sem torch, sem TTS instalado).

## Uso legal

A clonagem é local — o áudio nunca sai da máquina. Mesmo assim, evite
publicar saídas que se passem por pessoas reais sem permissão.
