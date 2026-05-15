function getCsrfToken() {
  const token = document.querySelector('input[name="csrfmiddlewaretoken"]');
  return token ? token.value : '';
}

function formatMultiplier(value) {
  return `${value.toFixed(2)}x`;
}

function animateFlightProgress(progressElement, multiplierElement, messageElement, crashValue, cashoutButton) {
  let multiplier = 1.0;
  const increment = 0.04;
  const updateInterval = 40;
  let tick = 0;

  cashoutButton.disabled = false;
  messageElement.textContent = 'Cash out before the flight crashes.';

  const interval = setInterval(() => {
    tick += 1;
    multiplier = 1 + tick * increment;
    multiplier = Math.min(multiplier, crashValue + 0.05);
    const percentage = Math.min((multiplier / (crashValue + 2)) * 100, 100);

    progressElement.style.width = `${percentage}%`;
    multiplierElement.textContent = formatMultiplier(multiplier);

    if (multiplier >= crashValue) {
      clearInterval(interval);
      multiplierElement.textContent = formatMultiplier(crashValue);
      cashoutButton.disabled = true;
      messageElement.textContent = 'Flight crashed. Round over.';
    }
  }, updateInterval);

  return interval;
}

function attachPlayWidget() {
  const playForm = document.querySelector('[data-play-widget]') || document.getElementById('play-form');
  const multiplierValue = document.getElementById('multiplier');
  const progressBar = document.getElementById('flight-progress');
  const flightCurve = document.getElementById('flight-curve');
  const flightFill = document.getElementById('flight-fill');
  const plane = document.getElementById('plane-silhouette');
  const cashoutButton = document.getElementById('cashout-button');
  const gameMessage = document.getElementById('game-message');
  const chatForm = document.getElementById('chat-form');
  const chatList = document.getElementById('chat-list');
  let currentSession = null;
  let flightTimer = null;

  if (!playForm) {
    return;
  }

  if (chatForm && chatList) {
    chatForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const formData = new FormData(chatForm);
      const response = await fetch(window.aviatorConfig.playUrl, {
        method: 'POST',
        headers: {
          'X-CSRFToken': getCsrfToken(),
        },
        body: formData,
      });
      const payload = await response.json();
      if (!payload.success) {
        return;
      }
      const item = document.createElement('div');
      item.className = 'chat-item';
      item.innerHTML = `<strong>${payload.message.username}</strong><span>${payload.message.text}</span>`;
      chatList.prepend(item);
      chatForm.reset();
    });
  }

  const professionalWidget = playForm.hasAttribute('data-play-widget');
  if (professionalWidget) {
    const slips = [...playForm.querySelectorAll('.aviator-bet-slip')];
    const startButtons = [...playForm.querySelectorAll('.start-bet')];
    const cashoutButtons = [...playForm.querySelectorAll('.cashout-bet')];
    const movingParts = [flightCurve, flightFill, plane].filter(Boolean);
    const fairnessStrip = document.getElementById('fairness-strip');
    const flightStage = document.querySelector('.flight-stage');
    const countdown = document.getElementById('round-countdown');
    const roundStrip = document.querySelector('.round-strip');
    const ledgerList = document.getElementById('ledger-list');
    const totalBetsCount = document.getElementById('total-bets-count');
    const ledgerTabs = [...document.querySelectorAll('[data-ledger-tab]')];
    const currency = window.aviatorConfig.currency || 'UGX';
    let currentMultiplier = 1.0;
    let queuedSlips = new Set();
    let activeSlipSessions = new Map();
    let activeSlipRows = new Map();
    let cashedOutSlips = new Set();
    let pendingCashoutSlips = new Set();
    let autoSlips = new Set();
    let phase = 'betting';
    let publicRound = null;
    let currentLedgerTab = 'all';
    let publicRows = [];
    let myRows = [];

    function setFlightProgress(progress) {
      movingParts.forEach((part) => {
        part.style.setProperty('--progress', progress.toFixed(2));
      });
    }

    function syncBetLabels() {
      slips.forEach((slip) => {
        const input = slip.querySelector('.stake-input');
        const start = slip.querySelector('.start-bet strong');
        const cashout = slip.querySelector('.cashout-bet strong');
        const stake = Math.max(parseFloat(input.value) || 0, 0);
        if (start) {
          start.textContent = queuedSlips.has(slip) ? 'WAITING' : `${stake.toFixed(2)} ${currency}`;
        }
        if (cashout) {
          cashout.textContent = `${(stake * currentMultiplier).toFixed(2)} ${currency}`;
        }
        slip.classList.toggle('is-queued', queuedSlips.has(slip));
        slip.classList.toggle('is-running', activeSlipSessions.has(slip) && !cashedOutSlips.has(slip));
        slip.classList.toggle('is-auto', autoSlips.has(slip));
      });
    }

    function syncSlipStates() {
      cashoutButtons.forEach((button) => {
        const slip = button.closest('.aviator-bet-slip');
        button.disabled = !activeSlipSessions.has(slip) || cashedOutSlips.has(slip) || pendingCashoutSlips.has(slip) || phase !== 'flying';
      });
      startButtons.forEach((button) => {
        const slip = button.closest('.aviator-bet-slip');
        button.disabled = phase !== 'betting' || queuedSlips.has(slip);
      });
      syncBetLabels();
    }

    function resetRound() {
      currentSession = null;
      currentMultiplier = 1.0;
      activeSlipSessions.clear();
      activeSlipRows.clear();
      cashedOutSlips.clear();
      pendingCashoutSlips.clear();
      multiplierValue.textContent = '1.00x';
      setFlightProgress(0);
      syncSlipStates();
    }

    function setFairnessText(label, value) {
      if (!fairnessStrip) {
        return;
      }
      fairnessStrip.innerHTML = `<span>${label}: ${value}</span><strong>Provably fair</strong>`;
    }

    function randomCrashPoint() {
      const r = Math.random();
      const crash = Math.max(1, 0.99 / (1 - Math.min(r, 0.985)));
      return Math.min(crash, 40);
    }

    function addHistoryPill(crashPoint) {
      if (!roundStrip) {
        return;
      }
      const pill = document.createElement('span');
      pill.className = `round-pill ${crashPoint >= 10 ? 'hot' : crashPoint >= 2 ? 'mid' : ''}`;
      pill.textContent = formatMultiplier(crashPoint);
      roundStrip.prepend(pill);
      [...roundStrip.querySelectorAll('.round-pill')].slice(18).forEach((item) => item.remove());
    }

    function shortName(index) {
      const letters = ['m', 'r', 's', 'a', 'l', 'k', 'n', 't', 'p', 'j'];
      return `${letters[index % letters.length]}***${(index % 9) + 1}`;
    }

    function rowTemplate(row) {
      return `
        <div class="ledger-row ${row.status === 'won' ? 'won' : 'muted'}">
          <span class="ledger-user"><span class="player-avatar">${row.avatar || ''}</span>${row.user}</span>
          <span>${row.bet.toFixed(2)}</span>
          <span>${row.multiplier ? formatMultiplier(row.multiplier) : ''}</span>
          <span>${row.win ? row.win.toFixed(2) : ''}</span>
        </div>
      `;
    }

    function renderLedger() {
      if (!ledgerList) {
        return;
      }
      let rows = publicRows;
      if (currentLedgerTab === 'mine') {
        rows = myRows;
      }
      if (currentLedgerTab === 'top') {
        rows = [...publicRows, ...myRows].sort((a, b) => b.bet - a.bet).slice(0, 28);
      }
      ledgerList.innerHTML = rows.length ? rows.map(rowTemplate).join('') : '<div class="ledger-empty">No bets in this view yet.</div>';
      if (totalBetsCount) {
        totalBetsCount.textContent = publicRows.length + queuedSlips.size + activeSlipSessions.size;
      }
    }

    ledgerTabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        currentLedgerTab = tab.dataset.ledgerTab || 'all';
        ledgerTabs.forEach((item) => item.classList.toggle('active', item === tab));
        renderLedger();
      });
    });

    function seedPublicBets() {
      const count = 18 + Math.floor(Math.random() * 16);
      const rows = [];
      for (let i = 0; i < count; i += 1) {
        const amount = [25, 20, 18, 15, 12, 10, 8, 5][Math.min(i, 7)] || 5;
        const cashed = phase === 'flying' && currentMultiplier > 1.15 && Math.random() > 0.6;
        const multiplier = cashed ? Math.min(currentMultiplier - 0.04, 1 + Math.random() * 2.4) : null;
        const user = shortName(i);
        rows.push({
          user,
          avatar: user.slice(0, 1).toUpperCase(),
          bet: amount,
          multiplier,
          win: multiplier ? amount * multiplier : 0,
          status: cashed ? 'won' : 'pending',
        });
      }
      publicRows = rows;
      renderLedger();
    }

    async function revealCrashedRound(attempt = 0) {
      if (!currentSession) {
        return;
      }

      const formData = new FormData();
      formData.append('action', 'settle_crash');
      formData.append('round_id', currentSession.round_id);

      const response = await fetch(window.aviatorConfig.playUrl, {
        method: 'POST',
        headers: {
          'X-CSRFToken': getCsrfToken(),
        },
        body: formData,
      });
      const payload = await response.json();
      if (payload.success && payload.fairness) {
        setFairnessText('Seed', payload.fairness.server_seed);
        return;
      }
      if (attempt < 8) {
        await new Promise((resolve) => setTimeout(resolve, 200));
        return revealCrashedRound(attempt + 1);
      }
    }

    slips.forEach((slip) => {
      const input = slip.querySelector('.stake-input');
      const modeButtons = [...slip.querySelectorAll('.bet-mode-toggle')];
      modeButtons.forEach((button) => {
        button.addEventListener('click', () => {
          const autoMode = button.dataset.mode === 'auto';
          if (autoMode) {
            autoSlips.add(slip);
            gameMessage.textContent = 'Auto bet enabled for this panel.';
          } else {
            autoSlips.delete(slip);
            queuedSlips.delete(slip);
            gameMessage.textContent = 'Manual bet mode enabled.';
          }
          modeButtons.forEach((item) => item.classList.toggle('active', item === button));
          syncSlipStates();
          renderLedger();
        });
      });
      slip.querySelectorAll('[data-stake]').forEach((button) => {
        button.addEventListener('click', () => {
          input.value = parseFloat(button.dataset.stake).toFixed(2);
          syncBetLabels();
        });
      });
      const minus = slip.querySelector('.amount-minus');
      const plus = slip.querySelector('.amount-plus');
      minus.addEventListener('click', () => {
        input.value = Math.max(1, (parseFloat(input.value) || 1) - 1).toFixed(2);
        syncBetLabels();
      });
      plus.addEventListener('click', () => {
        input.value = ((parseFloat(input.value) || 0) + 1).toFixed(2);
        syncBetLabels();
      });
      input.addEventListener('input', syncBetLabels);
    });

    async function createServerRoundFromQueue() {
      const stakes = [...queuedSlips]
        .map((slip) => parseFloat(slip.querySelector('.stake-input').value))
        .filter((stake) => stake > 0);
      if (!stakes.length) {
        return null;
      }
      const formData = new FormData();
      formData.append('action', 'start');
      formData.append('bet_data', JSON.stringify(stakes));

      const response = await fetch(window.aviatorConfig.playUrl, {
        method: 'POST',
        headers: {
          'X-CSRFToken': getCsrfToken(),
        },
        body: formData,
      });

      const payload = await response.json();
      if (!payload.success) {
        gameMessage.textContent = payload.message || 'Unable to begin the round.';
        queuedSlips.clear();
        syncBetLabels();
        return null;
      }
      return payload;
    }

    function queueBet(slip) {
      if (phase !== 'betting') {
        gameMessage.textContent = 'Wait for the next betting window.';
        return;
      }
      if (activeSlipSessions.has(slip)) {
        return;
      }
      queuedSlips.add(slip);
      gameMessage.textContent = 'Bet accepted for the next flight.';
      syncSlipStates();
      renderLedger();
    }

    async function startFlight() {
      if (flightTimer) {
        clearInterval(flightTimer);
      }

      phase = 'starting';
      syncSlipStates();
      gameMessage.textContent = queuedSlips.size ? 'Bets closed. Starting round...' : 'Bets closed. Watch the flight.';
      publicRound = {
        crash_point: randomCrashPoint(),
      };

      const enteredSlips = [...queuedSlips];
      const payload = await createServerRoundFromQueue();
      if (payload) {
        currentSession = payload;
        publicRound = payload;
        activeSlipSessions.clear();
        activeSlipRows.clear();
        enteredSlips.forEach((slip, index) => {
          if (payload.session_ids[index]) {
            activeSlipSessions.set(slip, payload.session_ids[index]);
            const amount = Math.max(parseFloat(slip.querySelector('.stake-input').value) || 0, 0);
            const row = {
              user: 'You',
              avatar: 'Y',
              bet: amount,
              multiplier: null,
              win: 0,
              status: 'pending',
              sessionId: payload.session_ids[index],
            };
            myRows.unshift(row);
            activeSlipRows.set(slip, row);
          }
        });
        myRows = myRows.slice(0, 40);
        setFairnessText('Hash', payload.server_seed_hash);
        const balanceDisplay = document.querySelector('#play-balance strong');
        if (balanceDisplay) {
          balanceDisplay.textContent = payload.balance.toFixed(2);
        }
      } else {
        currentSession = null;
        if (fairnessStrip) {
          fairnessStrip.innerHTML = '<span>Public demo round - no active bet</span><strong>Live table</strong>';
        }
      }

      queuedSlips.clear();
      currentMultiplier = 1.0;
      cashedOutSlips.clear();
      multiplierValue.textContent = '1.00x';
      setFlightProgress(0);
      phase = 'flying';
      syncSlipStates();

      gameMessage.textContent = activeSlipSessions.size ? 'Cash out before the flight crashes.' : 'Watching live round. Place a bet next round.';
      let tick = 0;
      flightTimer = setInterval(() => {
        tick += 1;
        currentMultiplier = Math.min(1 + tick * 0.04, publicRound.crash_point + 0.05);
        const progress = Math.min((currentMultiplier / Math.max(publicRound.crash_point, 2.4)) * 100, 100);
        multiplierValue.textContent = formatMultiplier(currentMultiplier);
        setFlightProgress(progress);
        syncBetLabels();
        activeSlipRows.forEach((row, slip) => {
          if (cashedOutSlips.has(slip)) {
            return;
          }
          row.multiplier = currentMultiplier;
          row.win = row.bet * currentMultiplier;
        });
        autoSlips.forEach((slip) => {
          if (!activeSlipSessions.has(slip) || cashedOutSlips.has(slip)) {
            return;
          }
          const target = Math.max(parseFloat(slip.querySelector('.auto-cashout-input')?.value) || 0, 0);
          if (target >= 1.01 && currentMultiplier >= target) {
            const button = slip.querySelector('.cashout-bet');
            if (button && !button.disabled) {
              cashOutRound(button);
            }
          }
        });
        if (tick % 12 === 0) {
          seedPublicBets();
        } else if (currentLedgerTab === 'mine') {
          renderLedger();
        }

        if (currentMultiplier >= publicRound.crash_point) {
          clearInterval(flightTimer);
          multiplierValue.textContent = formatMultiplier(publicRound.crash_point);
          gameMessage.textContent = `Flew away at ${formatMultiplier(publicRound.crash_point)}. Round over.`;
          addHistoryPill(publicRound.crash_point);
          const done = currentSession ? revealCrashedRound() : Promise.resolve();
          done.finally(() => {
            activeSlipRows.forEach((row, slip) => {
              if (!cashedOutSlips.has(slip)) {
                row.status = 'lost';
                row.multiplier = null;
                row.win = 0;
              }
            });
            renderLedger();
            phase = 'crashed';
            currentSession = null;
            activeSlipSessions.clear();
            cashedOutSlips.clear();
            activeSlipRows.clear();
            pendingCashoutSlips.clear();
            syncSlipStates();
            setTimeout(startBettingWindow, 1200);
          });
        }
      }, 40);
    }

    function startBettingWindow() {
      phase = 'betting';
      resetRound();
      autoSlips.forEach((slip) => queueBet(slip));
      seedPublicBets();
      if (flightStage) {
        flightStage.classList.add('is-betting');
      }
      if (countdown) {
        countdown.style.display = 'grid';
      }
      let secondsLeft = 4;
      gameMessage.textContent = 'Place your bets for the next round.';
      const interval = setInterval(() => {
        if (countdown) {
          countdown.querySelector('strong').textContent = secondsLeft;
        }
        secondsLeft -= 1;
        if (secondsLeft < 0) {
          clearInterval(interval);
          if (flightStage) {
            flightStage.classList.remove('is-betting');
          }
          if (countdown) {
            countdown.style.display = '';
          }
          startFlight();
        }
      }, 1000);
    }

    async function cashOutRound(button) {
      const slip = button.closest('.aviator-bet-slip');
      const sessionId = activeSlipSessions.get(slip);
      if (!currentSession || !sessionId || cashedOutSlips.has(slip) || pendingCashoutSlips.has(slip)) {
        return;
      }
      pendingCashoutSlips.add(slip);
      syncSlipStates();

      const formData = new FormData();
      formData.append('action', 'cashout');
      formData.append('session_ids', String(sessionId));
      formData.append('cashout_multiplier', currentMultiplier.toFixed(2));

      const response = await fetch(window.aviatorConfig.playUrl, {
        method: 'POST',
        headers: {
          'X-CSRFToken': getCsrfToken(),
        },
        body: formData,
      });

      const payload = await response.json();
      if (!payload.success) {
        pendingCashoutSlips.delete(slip);
        syncSlipStates();
        gameMessage.textContent = payload.message || 'Unable to cash out.';
        return;
      }

      pendingCashoutSlips.delete(slip);
      gameMessage.textContent = payload.message;
      const balanceDisplay = document.querySelector('#play-balance strong');
      if (balanceDisplay) {
        balanceDisplay.textContent = payload.balance.toFixed(2);
      }
      cashedOutSlips.add(slip);
      const row = activeSlipRows.get(slip);
      if (row) {
        row.multiplier = payload.cashout_multiplier;
        row.win = payload.won_amount;
        row.status = payload.status === 'won' ? 'won' : 'lost';
      }
      activeSlipSessions.delete(slip);
      activeSlipRows.delete(slip);
      slip.classList.remove('is-running');
      syncSlipStates();
      renderLedger();
    }

    startButtons.forEach((button) => {
      button.addEventListener('click', () => queueBet(button.closest('.aviator-bet-slip')));
    });
    cashoutButtons.forEach((button) => {
      button.addEventListener('click', () => cashOutRound(button));
    });
    startBettingWindow();
    return;
  }

  const gamePanel = document.getElementById('game-panel');
  const legacyCashoutButton = cashoutButton;
  const legacyProgressBar = progressBar;

  playForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    gamePanel.classList.remove('hidden');
    legacyCashoutButton.disabled = true;

    const formData = new FormData(playForm);
    formData.append('action', 'start');

    const response = await fetch(window.aviatorConfig.playUrl, {
      method: 'POST',
      headers: {
        'X-CSRFToken': getCsrfToken(),
      },
      body: formData,
    });

    const payload = await response.json();
    if (!payload.success) {
      gameMessage.textContent = payload.message || 'Unable to begin the round.';
      return;
    }

    currentSession = payload;
    legacyProgressBar.style.width = '0%';
    multiplierValue.textContent = '1.00x';

    if (flightTimer) {
      clearInterval(flightTimer);
    }

    flightTimer = animateFlightProgress(legacyProgressBar, multiplierValue, gameMessage, payload.crash_point, legacyCashoutButton);
  });

  legacyCashoutButton.addEventListener('click', async () => {
    if (!currentSession) {
      return;
    }

    const currentMultiplier = parseFloat(multiplierValue.textContent.replace('x', ''));
    const formData = new FormData();
    formData.append('action', 'cashout');
    formData.append('session_ids', currentSession.session_ids.join(','));
    formData.append('cashout_multiplier', currentMultiplier.toFixed(2));

    const response = await fetch(window.aviatorConfig.playUrl, {
      method: 'POST',
      headers: {
        'X-CSRFToken': getCsrfToken(),
      },
      body: formData,
    });

    const payload = await response.json();
    if (!payload.success) {
      gameMessage.textContent = payload.message || 'Unable to cash out.';
      return;
    }

    legacyCashoutButton.disabled = true;
    if (payload.status === 'won') {
      gameMessage.textContent = `Cashed out at ${formatMultiplier(payload.cashout_multiplier)} and won ${payload.won_amount.toFixed(2)} ${window.aviatorConfig.currency || 'UGX'}.`;
    } else {
      gameMessage.textContent = 'The flight crashed before cash out. Try again.';
    }

    const balanceDisplay = document.querySelector('.dashboard-hero strong');
    if (balanceDisplay) {
      balanceDisplay.textContent = `${payload.balance.toFixed(2)}`;
    }

    currentSession = null;

    if (flightTimer) {
      clearInterval(flightTimer);
    }
  });
}

function attachRoomWidget() {
  if (!window.roomConfig) {
    return;
  }

  const startButton = document.getElementById('room-start');
  const cashoutButton = document.getElementById('room-cashout');
  const roomGamePanel = document.getElementById('room-game');
  const roomMultiplier = document.getElementById('room-multiplier');
  const roomProgress = document.getElementById('room-progress');
  const roomMessage = document.getElementById('room-message');
  const participantList = document.getElementById('room-participants');
  const joinForm = document.getElementById('join-room-form');

  let roomTimer = null;
  let crashPoint = null;
  let currentMultiplier = 1.0;
  let roomRunning = window.roomConfig.initialRoomStatus === 'running';

  async function updateParticipants(data) {
    if (!participantList) {
      return;
    }

    participantList.innerHTML = '';
    data.room.participants.forEach((player) => {
      const item = document.createElement('li');
      item.innerHTML = `<span>${player.username}</span><strong>${player.stake.toFixed(2)} ${window.aviatorConfig.currency || 'UGX'}</strong><span>${player.cashed_out_at ? player.cashed_out_at.toFixed(2) + 'x' : 'Pending'}</span>`;
      participantList.appendChild(item);
    });
  }

  async function pollRoomStatus() {
    const response = await fetch(window.roomConfig.roomStatusUrl, {
      headers: {
        'X-CSRFToken': getCsrfToken(),
      },
    });
    const payload = await response.json();
    if (!payload.success) {
      return;
    }

    updateParticipants(payload);

    if (payload.room.status === 'running' && !roomRunning) {
      roomRunning = true;
      crashPoint = payload.room.crash_point;
      roomGamePanel.classList.remove('hidden');
      if (cashoutButton) {
        cashoutButton.disabled = false;
      }
      roomMessage.textContent = 'Game in progress. Cash out before the crash.';
      if (roomTimer) {
        clearInterval(roomTimer);
      }
      roomTimer = animateFlightProgress(roomProgress, roomMultiplier, roomMessage, crashPoint, cashoutButton);
    }

    if (payload.room.status === 'ended' && roomRunning) {
      roomRunning = false;
      if (roomTimer) {
        clearInterval(roomTimer);
      }
      roomMessage.textContent = 'Game ended. Review the results below.';
    }
  }

  if (joinForm) {
    joinForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const formData = new FormData(joinForm);
      const response = await fetch(window.roomConfig.roomActionUrl, {
        method: 'POST',
        headers: {
          'X-CSRFToken': getCsrfToken(),
        },
        body: formData,
      });

      const payload = await response.json();
      if (payload.success) {
        window.location.reload();
        return;
      }

      roomMessage.textContent = payload.message || 'Unable to join room.';
    });
  }

  if (startButton) {
    startButton.addEventListener('click', async () => {
      const formData = new FormData();
      formData.append('action', 'start');

      const response = await fetch(window.roomConfig.roomActionUrl, {
        method: 'POST',
        headers: {
          'X-CSRFToken': getCsrfToken(),
        },
        body: formData,
      });

      const payload = await response.json();
      if (!payload.success) {
        roomMessage.textContent = payload.message || 'Unable to start the game.';
        return;
      }

      crashPoint = payload.crash_point;
      roomRunning = true;
      roomGamePanel.classList.remove('hidden');
      if (cashoutButton) {
        cashoutButton.disabled = false;
      }
      if (roomTimer) {
        clearInterval(roomTimer);
      }
      roomTimer = animateFlightProgress(roomProgress, roomMultiplier, roomMessage, crashPoint, cashoutButton);
    });
  }

  if (cashoutButton) {
    cashoutButton.addEventListener('click', async () => {
      const currentValue = parseFloat(roomMultiplier.textContent.replace('x', '')) || 1.0;
      const formData = new FormData();
      formData.append('action', 'cashout');
      formData.append('cashout_multiplier', currentValue.toFixed(2));

      const response = await fetch(window.roomConfig.roomActionUrl, {
        method: 'POST',
        headers: {
          'X-CSRFToken': getCsrfToken(),
        },
        body: formData,
      });

      const payload = await response.json();
      if (!payload.success) {
        roomMessage.textContent = payload.message || 'Unable to cash out.';
        return;
      }

      cashoutButton.disabled = true;
      roomMessage.textContent = payload.message;
      if (roomTimer) {
        clearInterval(roomTimer);
      }
      await pollRoomStatus();
    });
  }

  pollRoomStatus();
  setInterval(pollRoomStatus, 5000);
}

function attachAviatorMenu() {
  const openButton = document.getElementById('aviator-menu-button');
  const closeButton = document.getElementById('aviator-menu-close');
  const drawer = document.getElementById('aviator-menu-drawer');
  const backdrop = document.getElementById('aviator-menu-backdrop');
  if (!openButton || !drawer || !backdrop) {
    return;
  }

  function openMenu() {
    drawer.classList.add('is-open');
    drawer.setAttribute('aria-hidden', 'false');
    openButton.setAttribute('aria-expanded', 'true');
    backdrop.hidden = false;
  }

  function closeMenu() {
    drawer.classList.remove('is-open');
    drawer.setAttribute('aria-hidden', 'true');
    openButton.setAttribute('aria-expanded', 'false');
    backdrop.hidden = true;
  }

  openButton.addEventListener('click', () => {
    if (drawer.classList.contains('is-open')) {
      closeMenu();
    } else {
      openMenu();
    }
  });
  closeButton?.addEventListener('click', closeMenu);
  backdrop.addEventListener('click', closeMenu);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeMenu();
    }
  });

  drawer.querySelectorAll('[data-setting-toggle]').forEach((button) => {
    const key = `aviator:${button.dataset.settingToggle}`;
    const stored = localStorage.getItem(key);
    const enabled = stored === null ? true : stored === 'on';
    button.classList.toggle('is-on', enabled);
    if (button.dataset.settingToggle === 'animation') {
      document.body.classList.toggle('aviator-animation-off', !enabled);
    }

    button.addEventListener('click', () => {
      const next = !button.classList.contains('is-on');
      button.classList.toggle('is-on', next);
      localStorage.setItem(key, next ? 'on' : 'off');
      if (button.dataset.settingToggle === 'animation') {
        document.body.classList.toggle('aviator-animation-off', !next);
      }
    });
  });
}

function attachAviatorModals() {
  const backdrop = document.getElementById('aviator-modal-backdrop');
  const modals = Array.from(document.querySelectorAll('[data-aviator-modal]'));
  const triggers = document.querySelectorAll('[data-aviator-modal-open]');
  const menuDrawer = document.getElementById('aviator-menu-drawer');
  const menuBackdrop = document.getElementById('aviator-menu-backdrop');
  const menuButton = document.getElementById('aviator-menu-button');

  if (!backdrop || modals.length === 0 || triggers.length === 0) {
    return;
  }

  function closeMenu() {
    if (!menuDrawer || !menuBackdrop || !menuButton) {
      return;
    }
    menuDrawer.classList.remove('is-open');
    menuDrawer.setAttribute('aria-hidden', 'true');
    menuButton.setAttribute('aria-expanded', 'false');
    menuBackdrop.hidden = true;
  }

  function activeModal() {
    return modals.find((modal) => modal.classList.contains('is-open'));
  }

  function closeModal() {
    const current = activeModal();
    if (!current) {
      return;
    }
    current.classList.remove('is-open');
    current.setAttribute('aria-hidden', 'true');
    backdrop.hidden = true;
    document.body.classList.remove('aviator-modal-lock');
  }

  function openModal(name) {
    const target = modals.find((modal) => modal.dataset.aviatorModal === name);
    if (!target) {
      return;
    }

    closeMenu();
    modals.forEach((modal) => {
      modal.classList.remove('is-open');
      modal.setAttribute('aria-hidden', 'true');
    });
    backdrop.hidden = false;
    document.body.classList.add('aviator-modal-lock');
    target.classList.add('is-open');
    target.setAttribute('aria-hidden', 'false');
    target.querySelector('[data-aviator-modal-close]')?.focus({ preventScroll: true });
  }

  triggers.forEach((trigger) => {
    trigger.addEventListener('click', (event) => {
      event.preventDefault();
      openModal(trigger.dataset.aviatorModalOpen);
    });
  });

  document.querySelectorAll('[data-aviator-modal-switch]').forEach((button) => {
    button.addEventListener('click', () => openModal(button.dataset.aviatorModalSwitch));
  });

  document.querySelectorAll('[data-aviator-modal-close]').forEach((button) => {
    button.addEventListener('click', closeModal);
  });

  backdrop.addEventListener('click', closeModal);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeModal();
    }
  });
}

window.addEventListener('DOMContentLoaded', () => {
  attachPlayWidget();
  attachRoomWidget();
  attachAviatorMenu();
  attachAviatorModals();
});
