<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <style>
    html, body {
      margin: 0;
      padding: 0;
      height: 100%;
      position: relative;
      font-family: -apple-system, sans-serif;
    }

    #progress-container {
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 6px;
      background: rgba(128, 128, 128, 0.2);
      z-index: 9999;
    }
    #progress-bar {
      width: 0%;
      height: 100%;
      background-color: #ff4b4b;
      transition: width 0.1s ease-out;
    }

    /* Single-row toolbar that scrolls sideways. Never wrap it: the iframe
       height is a fixed budget computed in Python (pane_height + 70). */
    #toolbar {
      display: flex;
      flex-wrap: nowrap;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      align-items: center;
      gap: 6px;
      padding: 8px 8px 6px 8px;
      background: #f2f2f2;
      border-bottom: 1px solid #ddd;
      margin-top: 6px;
      scrollbar-width: thin;
    }
    .ctrl-btn, .speed-btn {
      flex-shrink: 0;
      border: 1px solid #ccc;
      background: #fff;
      color: #222;
      border-radius: 999px;
      padding: 6px 12px;
      font-size: 13px;
      cursor: pointer;
      white-space: nowrap;
    }
    .speed-btn.active {
      background: #ff4b4b;
      color: #fff;
      border-color: #ff4b4b;
    }
    .speed-group {
      display: flex;
      flex-shrink: 0;
      gap: 4px;
    }

    /* Overlay panel: floats over the article so it never changes iframe height */
    #settings-panel {
      display: none;
      position: absolute;
      left: 8px;
      right: 8px;
      max-width: 420px;
      box-sizing: border-box;
      background: #ffffff;
      color: #222;
      border: 1px solid #ccc;
      border-radius: 12px;
      padding: 12px;
      z-index: 5000;
      box-shadow: 0 6px 20px rgba(0, 0, 0, 0.25);
    }
    #settings-panel.open { display: block; }
    #settings-panel label {
      display: block;
      font-size: 13px;
      font-weight: 600;
      margin: 10px 0 4px 0;
    }
    #settings-panel label:first-child { margin-top: 0; }
    #settings-panel select {
      width: 100%;
      font-size: 16px;
      padding: 6px;
      box-sizing: border-box;
    }
    #settings-panel input[type=range] { width: 100%; }
    #sleepStatus {
      font-size: 12px;
      color: #777;
      margin-top: 6px;
      min-height: 14px;
    }

    #reader-scroll {
      height: {{ pane_height }}px;
      overflow-y: auto;
      -webkit-overflow-scrolling: touch;
      box-sizing: border-box;
    }
    .reader-frame {
      font-family: {{ font_family }};
      font-size: {{ font_size }}px;
      {{ theme_style }}
      padding: 1.25rem 1rem;
    }
    .reader-frame p {
      margin-bottom: 1.35em;
      line-height: 1.8;
    }
  </style>
</head>
<body>
  <div id="progress-container">
    <div id="progress-bar"></div>
  </div>

  <div id="toolbar">
    <button id="scrollToggleBtn" class="ctrl-btn">▶️ Start Scroll</button>
    <span class="speed-group">
      <button class="speed-btn" data-ms="70">Slow</button>
      <button class="speed-btn active" data-ms="40">Medium</button>
      <button class="speed-btn" data-ms="20">Fast</button>
    </span>
    <button id="ttsBtn" class="ctrl-btn">🔊 Read Aloud</button>
    <button id="settingsBtn" class="ctrl-btn">⚙️ Voice &amp; Timer</button>
  </div>

  <div id="settings-panel">
    <label for="voiceSelect">Voice</label>
    <select id="voiceSelect"></select>

    <label for="rateRange">Speech speed: <span id="rateLabel">1.0×</span></label>
    <input type="range" id="rateRange" min="0.6" max="1.8" step="0.1" value="1">

    <label for="sleepSelect">Sleep timer (stops scrolling and reading)</label>
    <select id="sleepSelect">
      <option value="0">Off</option>
      <option value="5">5 minutes</option>
      <option value="10">10 minutes</option>
      <option value="15">15 minutes</option>
      <option value="30">30 minutes</option>
      <option value="45">45 minutes</option>
      <option value="60">60 minutes</option>
    </select>
    <div id="sleepStatus"></div>
  </div>

  <div id="reader-scroll">
    <div class="reader-frame">
      {{ content }}
    </div>
  </div>

  <script>
    (function () {
      const scrollEl = document.getElementById('reader-scroll');
      const bar = document.getElementById('progress-bar');
      const toolbar = document.getElementById('toolbar');
      const scrollToggleBtn = document.getElementById('scrollToggleBtn');
      const ttsBtn = document.getElementById('ttsBtn');
      const settingsBtn = document.getElementById('settingsBtn');
      const panel = document.getElementById('settings-panel');
      const voiceSelect = document.getElementById('voiceSelect');
      const rateRange = document.getElementById('rateRange');
      const rateLabel = document.getElementById('rateLabel');
      const sleepSelect = document.getElementById('sleepSelect');
      const sleepStatus = document.getElementById('sleepStatus');
      const speedBtns = document.querySelectorAll('.speed-btn');
      const SETTINGS_LABEL = '⚙️ Voice & Timer';
      const synth = ('speechSynthesis' in window) ? window.speechSynthesis : null;

      // ---------- Progress bar ----------
      function updateProgress() {
        const total = scrollEl.scrollHeight - scrollEl.clientHeight;
        if (total > 0) {
          bar.style.width = (scrollEl.scrollTop / total * 100) + '%';
        }
      }
      scrollEl.addEventListener('scroll', updateProgress);
      updateProgress();

      // ---------- Auto-scroll ----------
      let scrollTimer = null;
      let scrollSpeedMs = 40;
      let scrollActive = false;

      function startScrolling() {
        if (scrollTimer) clearInterval(scrollTimer);
        scrollTimer = setInterval(function () {
          scrollEl.scrollBy({ top: 1, behavior: 'auto' });
        }, scrollSpeedMs);
      }
      function stopScrolling() {
        if (scrollTimer) {
          clearInterval(scrollTimer);
          scrollTimer = null;
        }
      }
      function setScrollActive(active) {
        scrollActive = active;
        if (active) {
          startScrolling();
          scrollToggleBtn.textContent = '⏸ Pause Scroll';
        } else {
          stopScrolling();
          scrollToggleBtn.textContent = '▶️ Start Scroll';
        }
      }

      scrollToggleBtn.addEventListener('click', function () {
        setScrollActive(!scrollActive);
      });
      speedBtns.forEach(function (btn) {
        btn.addEventListener('click', function () {
          speedBtns.forEach(function (b) { b.classList.remove('active'); });
          btn.classList.add('active');
          scrollSpeedMs = parseInt(btn.dataset.ms, 10);
          if (scrollActive) startScrolling();
        });
      });

      // ---------- Read aloud ----------
      const ttsFullText = {{ tts_text_json }};
      let ttsChunks = [];
      let ttsIndex = 0;
      let ttsState = 'idle';   // idle | playing | paused
      let ttsSession = 0;      // bumps on every start/pause so stale callbacks are ignored

      function setTtsUi() {
        if (ttsState === 'playing') ttsBtn.textContent = '⏸ Pause Reading';
        else if (ttsState === 'paused') ttsBtn.textContent = '▶️ Resume Reading';
        else ttsBtn.textContent = '🔊 Read Aloud';
      }

      function pushSplit(str, out) {
        while (str.length > 220) {
          let cut = str.lastIndexOf(' ', 220);
          if (cut < 80) cut = 220;
          out.push(str.slice(0, cut).trim());
          str = str.slice(cut);
        }
        if (str.trim()) out.push(str.trim());
      }

      function chunkText(text) {
        const out = [];
        text.split(/\n+/).forEach(function (line) {
          const sentences = line.match(/[^.!?]+[.!?]*/g) || [line];
          let current = '';
          sentences.forEach(function (s) {
            if (current && (current + s).length > 200) {
              pushSplit(current.trim(), out);
              current = s;
            } else {
              current += s;
            }
          });
          if (current.trim()) pushSplit(current.trim(), out);
        });
        return out.filter(function (c) { return c.length > 0; });
      }

      function selectedVoice() {
        if (!synth) return null;
        const voices = synth.getVoices();
        return voices.find(function (v) { return v.voiceURI === voiceSelect.value; }) || null;
      }

      function speakNext(session) {
        if (session !== ttsSession) return;
        if (ttsIndex >= ttsChunks.length) {
          ttsState = 'idle';
          ttsIndex = 0;
          setTtsUi();
          return;
        }
        const utter = new SpeechSynthesisUtterance(ttsChunks[ttsIndex]);
        const voice = selectedVoice();
        if (voice) { utter.voice = voice; utter.lang = voice.lang; }
        utter.rate = parseFloat(rateRange.value) || 1;
        utter.onend = function () {
          if (session !== ttsSession) return;
          ttsIndex += 1;
          speakNext(session);
        };
        utter.onerror = function () {
          if (session !== ttsSession) return;
          ttsState = 'paused';
          setTtsUi();
        };
        synth.speak(utter);
      }

      function speakFrom(index) {
        ttsSession += 1;
        const session = ttsSession;
        const wasBusy = synth.speaking || synth.pending;
        if (wasBusy) synth.cancel();
        ttsIndex = index;
        // Start synchronously when idle (iOS wants speak() inside the tap);
        // only delay when we had to cancel something first.
        if (wasBusy) setTimeout(function () { speakNext(session); }, 60);
        else speakNext(session);
      }

      function startTts() {
        ttsChunks = chunkText(ttsFullText);
        if (!ttsChunks.length) return;
        ttsState = 'playing';
        setTtsUi();
        speakFrom(0);
      }
      function pauseTts() {
        ttsSession += 1;
        synth.cancel();
        ttsState = 'paused';
        setTtsUi();
      }
      function resumeTts() {
        ttsState = 'playing';
        setTtsUi();
        speakFrom(ttsIndex);
      }

      if (!synth) {
        ttsBtn.disabled = true;
        ttsBtn.textContent = '🔇 Read aloud unavailable';
        voiceSelect.disabled = true;
        rateRange.disabled = true;
      } else {
        ttsBtn.addEventListener('click', function () {
          if (ttsState === 'idle') startTts();
          else if (ttsState === 'playing') pauseTts();
          else resumeTts();
        });
      }

      // ---------- Voice list ----------
      function loadVoices() {
        if (!synth) return;
        const voices = synth.getVoices();
        if (!voices.length) return;
        const previous = voiceSelect.value;
        const navLang = (navigator.language || 'en').toLowerCase().split('-')[0];
        const sorted = voices.slice().sort(function (a, b) {
          const am = a.lang.toLowerCase().indexOf(navLang) === 0 ? 0 : 1;
          const bm = b.lang.toLowerCase().indexOf(navLang) === 0 ? 0 : 1;
          return am - bm || a.name.localeCompare(b.name);
        });
        voiceSelect.innerHTML = '';
        sorted.forEach(function (v) {
          const opt = document.createElement('option');
          opt.value = v.voiceURI;
          opt.textContent = v.name + ' (' + v.lang + ')';
          voiceSelect.appendChild(opt);
        });
        const stillThere = sorted.some(function (v) { return v.voiceURI === previous; });
        if (previous && stillThere) {
          voiceSelect.value = previous;
        } else {
          const preferred = sorted.find(function (v) {
            return v.default && v.lang.toLowerCase().indexOf(navLang) === 0;
          }) || sorted[0];
          voiceSelect.value = preferred.voiceURI;
        }
      }
      if (synth) {
        loadVoices();
        if (synth.onvoiceschanged !== undefined) synth.onvoiceschanged = loadVoices;
        setTimeout(loadVoices, 500);   // some browsers never fire voiceschanged
      }

      function restartIfPlaying() {
        if (synth && ttsState === 'playing') speakFrom(ttsIndex);
      }
      voiceSelect.addEventListener('change', restartIfPlaying);
      rateRange.addEventListener('input', function () {
        rateLabel.textContent = parseFloat(rateRange.value).toFixed(1) + '×';
      });
      rateRange.addEventListener('change', restartIfPlaying);

      // ---------- Settings panel ----------
      settingsBtn.addEventListener('click', function () {
        if (!panel.classList.contains('open')) {
          panel.style.top = (toolbar.offsetTop + toolbar.offsetHeight + 4) + 'px';
        }
        panel.classList.toggle('open');
      });
      scrollEl.addEventListener('pointerdown', function () {
        panel.classList.remove('open');
      });

      // ---------- Sleep timer ----------
      let sleepEndsAt = 0;
      let sleepInterval = null;

      function formatClock(ms) {
        const total = Math.max(0, Math.round(ms / 1000));
        const m = Math.floor(total / 60);
        const s = total % 60;
        return m + ':' + (s < 10 ? '0' : '') + s;
      }

      function stopEverything() {
        setScrollActive(false);
        if (synth && ttsState === 'playing') pauseTts();
      }

      function clearSleepTimer() {
        if (sleepInterval) { clearInterval(sleepInterval); sleepInterval = null; }
        sleepEndsAt = 0;
        sleepSelect.value = '0';
        sleepStatus.textContent = '';
        settingsBtn.textContent = SETTINGS_LABEL;
      }

      function tickSleep() {
        if (!sleepEndsAt) return;
        const remaining = sleepEndsAt - Date.now();
        if (remaining <= 0) {
          stopEverything();
          clearSleepTimer();
          sleepStatus.textContent = 'Timer finished. Playback stopped.';
          return;
        }
        sleepStatus.textContent = 'Stopping in ' + formatClock(remaining);
        settingsBtn.textContent = '⏲ ' + formatClock(remaining);
      }

      sleepSelect.addEventListener('change', function () {
        const minutes = parseInt(sleepSelect.value, 10);
        if (sleepInterval) { clearInterval(sleepInterval); sleepInterval = null; }
        if (!minutes) {
          clearSleepTimer();
          return;
        }
        sleepEndsAt = Date.now() + minutes * 60000;
        sleepInterval = setInterval(tickSleep, 1000);
        tickSleep();
      });
    })();
  </script>
</body>
</html>
