// Voz: STT contínuo (Web Speech API com auto-restart) + TTS estilo JARVIS.
// Inclui workarounds dos bugs conhecidos do Chrome:
//   - speechSynthesis trava em utterances subsequentes -> keep-alive (pause/resume)
//   - utterance pode ser GC antes de disparar onend -> manter ref
//   - cancel() seguido de speak() imediato perde callbacks -> pequeno delay

class Voice {
    constructor() {
        this.recognition = null;
        this.synthVoice = null;
        this.listening = false;       // sessão de STT ativa
        this.alwaysOn = false;         // modo "sempre ligado"
        this.suspended = false;        // pausado (durante TTS pra evitar feedback)
        this.speaking = false;         // JARVIS está falando agora
        this._restartTimer = null;
        this._fakeAudioId = null;
        this._keepAliveId = null;
        this._currentUtt = null;
        this._safetyTimer = null;

        // TTS backend (Coqui XTTS-v2 local)
        this._currentAudio = null;     // <audio> element atualmente tocando
        this._backendTTSDisabled = false; // se já falhou uma vez, desliga até próxima sessão

        // Callbacks
        this.onTranscript    = null;
        this.onListenStart   = null;
        this.onListenEnd     = null;
        this.onSpeakStart    = null;
        this.onSpeakEnd      = null;
        this.onAudioFrame    = null;
        this.onAlwaysOnChange = null;

        this._initRecognition();
        this._initSynthesis();
    }

    _initRecognition() {
        const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SR) {
            console.warn('[voice] SpeechRecognition indisponível.');
            return;
        }
        const rec = new SR();
        rec.lang = 'pt-BR';
        rec.continuous = true;        // <-- SEMPRE LIGADO
        rec.interimResults = false;
        rec.maxAlternatives = 1;

        rec.onstart = () => {
            this.listening = true;
            console.log('[voice] STT start');
            if (this.onListenStart) this.onListenStart();
        };

        rec.onresult = (e) => {
            for (let i = e.resultIndex; i < e.results.length; i++) {
                const res = e.results[i];
                if (res.isFinal) {
                    const text = (res[0].transcript || '').trim();
                    if (text && this.onTranscript) {
                        console.log('[voice] transcript:', text);
                        this.onTranscript(text);
                    }
                }
            }
        };

        rec.onerror = (e) => {
            console.warn('[voice] STT error:', e.error);
            // permissão negada -> desliga sempre-ligado
            if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
                this.alwaysOn = false;
                if (this.onAlwaysOnChange) this.onAlwaysOnChange(false);
            }
            // 'no-speech', 'audio-capture', 'network' -> auto-restart pelo onend
        };

        rec.onend = () => {
            this.listening = false;
            console.log('[voice] STT end (alwaysOn=' + this.alwaysOn + ', suspended=' + this.suspended + ')');
            if (this.onListenEnd) this.onListenEnd();
            if (this.alwaysOn && !this.suspended) {
                this._scheduleRestart(250);
            }
        };

        this.recognition = rec;
    }

    _initSynthesis() {
        const pickVoice = () => {
            const voices = speechSynthesis.getVoices();
            if (!voices.length) return;
            const pt = voices.filter(v => v.lang && v.lang.toLowerCase().startsWith('pt'));

            // Prioridade: vozes neurais (Online/Natural) masculinas pt-BR > Daniel/Antonio padrão
            const tests = [
                v => /antonio.*(natural|online)|antonio.*neural/i.test(v.name) && /pt-?BR/i.test(v.lang),
                v => /daniel.*(natural|online)|daniel.*neural/i.test(v.name) && /pt-?BR/i.test(v.lang),
                v => /(natural|online|neural)/i.test(v.name) && /pt-?BR/i.test(v.lang),
                v => /(natural|online|neural)/i.test(v.name) && /pt/i.test(v.lang),
                v => /antonio|daniel|paulo|ricardo|francisco/i.test(v.name),
                v => v.lang === 'pt-BR',
                v => v.lang.toLowerCase().startsWith('pt'),
            ];
            for (const test of tests) {
                const found = pt.find(test) || voices.find(test);
                if (found) {
                    this.synthVoice = found;
                    console.log('[voice] usando:', found.name, '/', found.lang);
                    return;
                }
            }
            this.synthVoice = voices[0];
        };
        pickVoice();
        if (typeof speechSynthesis !== 'undefined' && speechSynthesis.onvoiceschanged !== undefined) {
            speechSynthesis.onvoiceschanged = pickVoice;
        }
    }

    // --- API pública ---

    enableAlwaysOn() {
        if (!this.recognition) return false;
        if (this.alwaysOn) return true;
        this.alwaysOn = true;
        this._tryStart();
        if (this.onAlwaysOnChange) this.onAlwaysOnChange(true);
        return true;
    }

    disableAlwaysOn() {
        this.alwaysOn = false;
        this._cancelRestart();
        if (this.recognition && this.listening) {
            try { this.recognition.stop(); } catch(_) {}
        }
        if (this.onAlwaysOnChange) this.onAlwaysOnChange(false);
    }

    /** Pausa o mic enquanto JARVIS fala (evita feedback). */
    suspend() {
        this.suspended = true;
        this._cancelRestart();
        if (this.recognition && this.listening) {
            try { this.recognition.abort(); } catch(_) {}
        }
    }

    resume() {
        this.suspended = false;
        if (this.alwaysOn) this._scheduleRestart(250);
    }

    speak(text) {
        if (!text) {
            console.warn('[voice] speak ignorado (texto vazio)');
            return;
        }

        // Limpa estado anterior — força reset, mesmo se algo travou.
        this._forceResetSpeech();

        // Tenta o TTS local (XTTS-v2 com voz clonada do JARVIS).
        // Se não responder ou tiver falhado uma vez, cai pra Web Speech API.
        if (!this._backendTTSDisabled) {
            this._doSpeakBackend(text).catch((err) => {
                console.warn('[voice] backend TTS falhou — fallback Web Speech:', err);
                this._backendTTSDisabled = true;
                if (window.speechSynthesis) {
                    setTimeout(() => this._doSpeak(text), 80);
                } else {
                    this._forceResetSpeech();
                }
            });
        } else if (window.speechSynthesis) {
            setTimeout(() => this._doSpeak(text), 80);
        }
    }

    async _doSpeakBackend(text) {
        // Inicia spinner/estado de fala antes mesmo de receber o áudio,
        // pra UI reagir (mic-off, orb pulsando).
        this.speaking = true;
        if (this.onSpeakStart) this.onSpeakStart();
        this.suspend();

        const resp = await fetch('/api/tts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, language: 'pt' }),
        });

        if (!resp.ok) {
            let detail = '';
            try { detail = (await resp.json()).error || ''; } catch (_) {}
            this.speaking = false;
            if (this.onSpeakEnd) this.onSpeakEnd();
            throw new Error(`HTTP ${resp.status}${detail ? ' — ' + detail : ''}`);
        }

        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        this._currentAudio = audio;

        // "Fake audio frames" pra waveform/orb reagirem enquanto toca.
        this._fakeAudio();

        return new Promise((resolve, reject) => {
            let finished = false;
            const finish = (reason) => {
                if (finished) return;
                finished = true;
                console.log('[voice] backend TTS finish:', reason);
                this.speaking = false;
                this._stopFakeAudio();
                this._currentAudio = null;
                try { URL.revokeObjectURL(url); } catch (_) {}
                if (this.onSpeakEnd) this.onSpeakEnd();
                this.resume();
                resolve();
            };

            audio.onended = () => finish('ended');
            audio.onerror = (e) => {
                console.warn('[voice] audio error:', e);
                finish('error');
                reject(new Error('audio playback error'));
            };

            audio.play().catch((err) => {
                console.warn('[voice] audio.play() rejeitou:', err);
                finish('play-reject');
                reject(err);
            });
        });
    }

    _doSpeak(text) {
        const utt = new SpeechSynthesisUtterance(text);
        if (this.synthVoice) utt.voice = this.synthVoice;
        utt.lang = 'pt-BR';
        utt.rate = 0.93;     // levemente mais lento, deliberado
        utt.pitch = 0.82;    // grave, estilo JARVIS
        utt.volume = 1.0;

        let finished = false;
        const finish = (reason) => {
            if (finished) return;
            finished = true;
            console.log('[voice] speak finish:', reason);
            this.speaking = false;
            this._stopKeepAlive();
            this._stopFakeAudio();
            this._stopSafetyTimer();
            this._currentUtt = null;
            if (this.onSpeakEnd) this.onSpeakEnd();
            this.resume();
        };

        utt.onstart = () => {
            console.log('[voice] speak start (' + text.length + ' chars)');
            this.speaking = true;
            if (this.onSpeakStart) this.onSpeakStart();
            this._fakeAudio();
            this._startKeepAlive();
        };
        utt.onend   = () => finish('onend');
        utt.onerror = (e) => {
            console.warn('[voice] speak error:', e && e.error);
            finish('onerror:' + (e && e.error));
        };

        this._currentUtt = utt;       // mantém ref (bug do GC)
        this.suspend();                // mic OFF durante a fala

        try {
            speechSynthesis.speak(utt);
        } catch (e) {
            console.error('[voice] speak throw:', e);
            finish('throw');
            return;
        }

        // Safety: se em 1.5s nada começar, tenta de novo (Chrome às vezes engole)
        this._stopSafetyTimer();
        this._safetyTimer = setTimeout(() => {
            if (!this.speaking && !finished) {
                console.warn('[voice] speak não iniciou — re-disparando');
                try {
                    speechSynthesis.cancel();
                    speechSynthesis.speak(utt);
                } catch (_) {}
            }
        }, 1500);
    }

    /** Interrompe a fala atual (ESC, clique no orb, etc). */
    cancel() {
        console.log('[voice] cancel()');
        this._forceResetSpeech();
    }

    _forceResetSpeech() {
        if (window.speechSynthesis) {
            try { speechSynthesis.cancel(); } catch (_) {}
        }
        if (this._currentAudio) {
            try { this._currentAudio.pause(); } catch (_) {}
            try { this._currentAudio.src = ''; } catch (_) {}
            this._currentAudio = null;
        }
        this.speaking = false;
        this._stopFakeAudio();
        this._stopKeepAlive();
        this._stopSafetyTimer();
        this._currentUtt = null;
        // Garante mic ON de novo se sempre-ligado.
        this.suspended = false;
        if (this.alwaysOn) this._scheduleRestart(200);
        if (this.onSpeakEnd) this.onSpeakEnd();
    }

    // --- Internos ---

    _tryStart() {
        if (!this.recognition) return;
        if (this.listening || this.suspended) return;
        try {
            this.recognition.start();
        } catch (e) {
            // Já iniciado, ou erro genérico — agenda nova tentativa.
            this._scheduleRestart(500);
        }
    }

    _scheduleRestart(delay) {
        this._cancelRestart();
        this._restartTimer = setTimeout(() => {
            this._restartTimer = null;
            if (this.alwaysOn && !this.suspended) this._tryStart();
        }, delay);
    }

    _cancelRestart() {
        if (this._restartTimer) {
            clearTimeout(this._restartTimer);
            this._restartTimer = null;
        }
    }

    _fakeAudio() {
        this._stopFakeAudio();
        this._fakeAudioId = setInterval(() => {
            if (this.onAudioFrame) this.onAudioFrame(0.4 + Math.random() * 0.6);
        }, 60);
    }

    _stopFakeAudio() {
        if (this._fakeAudioId) {
            clearInterval(this._fakeAudioId);
            this._fakeAudioId = null;
        }
        if (this.onAudioFrame) this.onAudioFrame(0);
    }

    /** Workaround do bug "speechSynthesis pausa após ~15s no Chrome". */
    _startKeepAlive() {
        this._stopKeepAlive();
        this._keepAliveId = setInterval(() => {
            if (window.speechSynthesis && speechSynthesis.speaking) {
                try {
                    speechSynthesis.pause();
                    speechSynthesis.resume();
                } catch (_) {}
            }
        }, 5000);
    }

    _stopKeepAlive() {
        if (this._keepAliveId) {
            clearInterval(this._keepAliveId);
            this._keepAliveId = null;
        }
    }

    _stopSafetyTimer() {
        if (this._safetyTimer) {
            clearTimeout(this._safetyTimer);
            this._safetyTimer = null;
        }
    }
}

window.voice = new Voice();
