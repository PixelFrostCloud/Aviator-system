function getCsrfToken() {
  const token = document.querySelector('input[name="csrfmiddlewaretoken"]');
  return token ? token.value : '';
}

function formatMultiplier(value) {
  return `${value.toFixed(2)}x`;
}

function escapeHtml(value) {
  const element = document.createElement('span');
  element.textContent = value || '';
  return element.innerHTML;
}

const FLIGHT_INCREMENT = 0.04;
const FLIGHT_TICK_MS = 40;

function calculateMaskedFlightProgress(tick) {
  const elapsedSeconds = tick * (FLIGHT_TICK_MS / 1000);
  const eased = 1 - Math.exp(-elapsedSeconds / 4.8);
  const steadyDrift = Math.min(elapsedSeconds * 1.35, 18);
  return Math.min((eased * 72) + steadyDrift, 96);
}

function calculateFlightMotion(progress, elapsedMs = 0, isFinal = false) {
  const ratio = Math.min(Math.max(progress / 96, 0), 1);
  const seconds = elapsedMs / 1000;
  const launch = Math.min(seconds / 1.12, 1);
  const groundRoll = Math.min(seconds / 0.42, 1);
  const lift = 1 - Math.pow(1 - Math.min(Math.max((seconds - 0.28) / 1.35, 0), 1), 2.4);
  const climb = 1 - Math.pow(1 - ratio, 2.05);
  const vertical = 1 - Math.pow(1 - ratio, 1.42);
  const cruise = Math.max(0, (ratio - 0.68) / 0.32);
  const cruiseEase = 1 - Math.pow(1 - Math.min(cruise, 1), 2);
  const bob = isFinal ? 0 : Math.sin(seconds * 3.6) * (0.65 + cruiseEase * 2.15);
  const glide = isFinal ? 0 : Math.sin(seconds * 1.55) * cruiseEase * 2.35;
  const noseTrim = Math.sin(seconds * 2.7) * cruiseEase * 1.7;
  const x = (groundRoll * 8.5) + Math.max(0, 66.5 * climb - 2.8) + glide;
  const y = Math.max(0, (60.5 * vertical * lift) + bob);

  return {
    x: Math.min(74, x),
    y: Math.min(64, y),
    angle: (groundRoll * 5) + (21 * launch) - (6 * cruiseEase) + noseTrim,
  };
}

function animateFlightProgress(progressElement, multiplierElement, messageElement, crashValue, cashoutButton) {
  let multiplier = 1.0;
  let tick = 0;

  cashoutButton.disabled = false;
  messageElement.textContent = 'Cash out before the flight crashes.';

  const interval = setInterval(() => {
    tick += 1;
    multiplier = 1 + tick * FLIGHT_INCREMENT;
    multiplier = Math.min(multiplier, crashValue + 0.05);
    const percentage = calculateMaskedFlightProgress(tick);

    progressElement.style.width = `${percentage}%`;
    multiplierElement.textContent = formatMultiplier(multiplier);

    if (multiplier >= crashValue) {
      clearInterval(interval);
      multiplierElement.textContent = formatMultiplier(crashValue);
      cashoutButton.disabled = true;
      messageElement.textContent = 'Flight crashed. Round over.';
    }
  }, FLIGHT_TICK_MS);

  return interval;
}

async function settleRoundById(roundId) {
  const formData = new FormData();
  formData.append('action', 'settle_crash');
  formData.append('round_id', roundId);

  const response = await fetch(window.aviatorConfig.playUrl, {
    method: 'POST',
    headers: {
      'X-CSRFToken': getCsrfToken(),
    },
    body: formData,
  });
  const payload = await response.json();
  return payload.success ? payload : null;
}

function animateServerFlightProgress(progressElement, multiplierElement, messageElement, session, cashoutButton) {
  let tick = 0;
  let checkInFlight = false;

  cashoutButton.disabled = false;
  messageElement.textContent = 'Cash out before the flight crashes.';

  const interval = setInterval(() => {
    tick += 1;
    const multiplier = 1 + tick * FLIGHT_INCREMENT;
    progressElement.style.width = `${calculateMaskedFlightProgress(tick)}%`;
    multiplierElement.textContent = formatMultiplier(multiplier);

    if (!checkInFlight && tick % 5 === 0) {
      checkInFlight = true;
      settleRoundById(session.round_id)
        .then((payload) => {
          if (!payload) {
            return;
          }
          clearInterval(interval);
          multiplierElement.textContent = formatMultiplier(payload.crash_point);
          cashoutButton.disabled = true;
          messageElement.textContent = 'Flight crashed. Round over.';
        })
        .finally(() => {
          checkInFlight = false;
        });
    }
  }, FLIGHT_TICK_MS);

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
    const chatPanel = document.querySelector('[data-chat-panel]');
    const chatInput = chatForm.querySelector('input[name="message"]');
    const charCount = document.getElementById('chat-char-count');
    const newMessagePill = document.getElementById('new-message-pill');
    const spamFilter = document.getElementById('chat-spam-filter');
    const rainForm = document.getElementById('chat-rain-form');
    const rainTotal = document.getElementById('rain-total');
    const rainTotalDisplay = document.getElementById('rain-total-display');
    const rainPlayers = document.getElementById('rain-players');
    const rainAmount = document.getElementById('rain-amount');
    const rainHelper = document.getElementById('rain-helper');
    const rulesModal = document.getElementById('chat-rules-modal');
    const onlineCount = document.getElementById('chat-online-count');
    const maxChatLength = parseInt(chatInput?.getAttribute('maxlength') || '160', 10);
    const playPage = document.querySelector('.aviator-play-page');
    const shell = document.querySelector('.aviator-shell');
    const reopenChatButton = document.createElement('button');
    reopenChatButton.className = 'chat-reopen-button';
    reopenChatButton.type = 'button';
    reopenChatButton.textContent = 'Chat';
    reopenChatButton.hidden = true;
    shell?.append(reopenChatButton);
    const chatDrawerBackdrop = document.createElement('button');
    chatDrawerBackdrop.className = 'chat-drawer-backdrop';
    chatDrawerBackdrop.type = 'button';
    chatDrawerBackdrop.setAttribute('aria-label', 'Close chat');
    chatDrawerBackdrop.hidden = true;
    document.body.append(chatDrawerBackdrop);
    const emojiPicker = document.createElement('section');
    emojiPicker.className = 'chat-emoji-picker';
    emojiPicker.hidden = true;
    emojiPicker.innerHTML = `
      <div class="emoji-search-wrap">
        <span aria-hidden="true">⌕</span>
        <input type="search" placeholder="Search emoji.." aria-label="Search emoji">
      </div>
      <div class="emoji-grid" role="listbox" aria-label="Emoji picker"></div>
    `;
    chatPanel?.append(emojiPicker);
    const emojiSearch = emojiPicker.querySelector('input');
    const emojiGrid = emojiPicker.querySelector('.emoji-grid');
    const emojiItems = [
      ['😀', 'grin happy smile'], ['😍', 'heart eyes love'], ['😜', 'wink tongue playful'], ['😔', 'sad disappointed'],
      ['😨', 'scared fear'], ['🥶', 'cold frozen'], ['😱', 'scream shock'], ['😖', 'frustrated'], ['😴', 'sleep sleepy'],
      ['😃', 'happy smile'], ['😘', 'kiss love'], ['😝', 'tongue laugh'], ['😊', 'soft smile'], ['😂', 'laugh tears'],
      ['😅', 'sweat smile'], ['😳', 'surprised blush'], ['😆', 'laugh closed'], ['😵', 'dizzy shock'],
      ['😃', 'grinning'], ['😚', 'kiss closed'], ['😛', 'tongue'], ['🙄', 'eyeroll'], ['😭', 'cry tears'],
      ['😟', 'worried'], ['😠', 'angry'], ['😊', 'smile'], ['😲', 'astonished'],
      ['🙂', 'slight smile'], ['😗', 'kiss'], ['😯', 'surprised'], ['😞', 'sad'], ['😪', 'sleepy tired'],
      ['😩', 'weary'], ['😡', 'rage angry'], ['🤭', 'hand mouth'], ['🥺', 'pleading'],
      ['☺️', 'relaxed smile'], ['😙', 'kiss smile'], ['😁', 'big grin'], ['😣', 'persevere'],
      ['😰', 'anxious sweat'], ['😫', 'tired'], ['😤', 'triumph'], ['😎', 'cool sunglasses'], ['😳', 'blush']
    ];
    if (rulesModal && rulesModal.parentElement !== document.body) {
      document.body.append(rulesModal);
    }

    chatList.querySelectorAll('.chat-row.win').forEach((row) => row.remove());
    chatList.querySelectorAll('.chat-bubble p').forEach((message) => {
      if (message.textContent.includes('W H A T') || message.textContent.includes('0118')) {
        message.closest('.chat-row')?.remove();
      }
    });
    if (!chatList.children.length) {
      const empty = document.createElement('div');
      empty.className = 'chat-empty';
      empty.textContent = 'No chat messages yet.';
      chatList.append(empty);
    }

    function isChatNearBottom() {
      return chatList.scrollHeight - chatList.scrollTop - chatList.clientHeight < 32;
    }

    function renderChatMessage(message, options = {}) {
      const wasNearBottom = isChatNearBottom();
      chatList.querySelector('.chat-empty')?.remove();
      const article = document.createElement('article');
      article.className = `chat-row${options.system ? ' system' : ''}`;
      article.innerHTML = `
        <span class="chat-avatar">${escapeHtml((message.username || 'B').charAt(0).toUpperCase())}</span>
        <div class="chat-bubble">
          <div class="chat-meta"><strong>${escapeHtml(message.username || 'player')}</strong><time>${escapeHtml(message.created_at || '')}</time></div>
          <p>${escapeHtml(message.text || '')}</p>
        </div>
        <button class="chat-like" type="button" aria-label="Like"><span>♥</span><b>1</b></button>
      `;
      chatList.append(article);
      const likeButton = article.querySelector('.chat-like');
      if (likeButton) {
        likeButton.dataset.messageId = message.id || '';
        const likeCount = likeButton.querySelector('b');
        if (likeCount) {
          likeCount.textContent = String(message.reaction_count || 0);
        }
      }
      if (options.local || wasNearBottom) {
        chatList.scrollTop = chatList.scrollHeight;
        newMessagePill.hidden = true;
      } else if (options.notify !== false) {
        newMessagePill.hidden = false;
      }
      return article;
    }

    function updateCharCount() {
      if (!chatInput || !charCount) {
        return;
      }
      charCount.textContent = Math.max(maxChatLength - chatInput.value.length, 0);
    }

    function renderEmojiGrid(filter = '') {
      if (!emojiGrid) {
        return;
      }
      const query = filter.trim().toLowerCase();
      emojiGrid.innerHTML = '';
      emojiItems
        .filter(([, keywords]) => !query || keywords.includes(query))
        .forEach(([emoji, keywords]) => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'emoji-choice';
          button.dataset.keywords = keywords;
          button.textContent = emoji;
          button.setAttribute('aria-label', keywords.split(' ')[0]);
          emojiGrid.append(button);
        });
    }

    function toggleEmojiPicker(forceOpen = null) {
      const shouldOpen = forceOpen === null ? emojiPicker.hidden : forceOpen;
      emojiPicker.hidden = !shouldOpen;
      chatPanel?.classList.toggle('emoji-open', shouldOpen);
      if (shouldOpen) {
        renderEmojiGrid(emojiSearch?.value || '');
        emojiSearch?.focus({ preventScroll: true });
      }
    }

    function insertEmoji(emoji) {
      if (!chatInput || !emoji) {
        return;
      }
      const start = chatInput.selectionStart ?? chatInput.value.length;
      const end = chatInput.selectionEnd ?? chatInput.value.length;
      const before = chatInput.value.slice(0, start);
      const after = chatInput.value.slice(end);
      const next = `${before}${emoji}${after}`.slice(0, maxChatLength);
      chatInput.value = next;
      const cursor = Math.min(start + emoji.length, next.length);
      chatInput.focus();
      chatInput.setSelectionRange(cursor, cursor);
      updateCharCount();
    }

    function showSpamFilter() {
      if (!spamFilter) {
        return;
      }
      spamFilter.hidden = false;
      setTimeout(() => {
        spamFilter.hidden = true;
      }, 2200);
    }

    function walletBalance() {
      return Math.max(parseFloat(window.aviatorConfig.walletBalance || '0') || 0, 0);
    }

    function formatMoney(value) {
      return `${Number(value).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })} ${window.aviatorConfig.currency || 'UGX'}`;
    }

    function setRainHelper(message, state = 'neutral') {
      if (!rainHelper) {
        return;
      }
      rainHelper.textContent = message || 'This feature will give selected amount to random users in chat.';
      rainHelper.dataset.state = state;
    }

    function syncRainAmounts(source) {
      if (!rainTotal || !rainPlayers || !rainAmount) {
        return;
      }
      const maxBalance = walletBalance();
      const maxPlayers = Math.max(parseInt(rainPlayers.max || '20', 10) || 20, 1);
      const players = Math.min(Math.max(parseInt(rainPlayers.value, 10) || 1, 1), maxPlayers);
      let amount = Math.max(parseFloat(rainAmount.value) || 0.01, 0.01);
      let total = Math.max(parseFloat(rainTotal.value) || 0, 0);
      if (source === 'open' || source === 'balance') {
        total = Math.min(Math.max(maxBalance, 0), 1500);
        if (total < 1 && maxBalance > 0) {
          total = maxBalance;
        }
      }
      if (maxBalance > 0) {
        total = Math.min(total || maxBalance, maxBalance);
      }
      if (source === 'open' || source === 'balance' || source === 'total' || source === 'players') {
        amount = players > 0 ? total / players : 0;
      } else if (source === 'amount') {
        total = amount * players;
        if (maxBalance > 0 && total > maxBalance) {
          total = maxBalance;
          amount = total / players;
        }
      }
      rainPlayers.value = players.toString();
      rainTotal.max = maxBalance.toFixed(2);
      rainAmount.value = Math.max(amount, 0).toFixed(2);
      rainTotal.value = Math.max(total, 0).toFixed(2);
      if (rainTotalDisplay) {
        rainTotalDisplay.textContent = formatMoney(total);
      }
      if (maxBalance < 1) {
        setRainHelper('You do not have sufficient balance on your wallet.', 'error');
      } else if (total > maxBalance) {
        setRainHelper('You do not have sufficient balance on your wallet.', 'error');
      } else {
        setRainHelper(`Wallet balance: ${formatMoney(maxBalance)}.`, 'neutral');
      }
    }

    chatInput?.addEventListener('input', updateCharCount);
    updateCharCount();

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
        showSpamFilter();
        return;
      }
      renderChatMessage(payload.message, { local: true });
      if (payload.message.flagged) {
        showSpamFilter();
      }
      chatForm.reset();
      toggleEmojiPicker(false);
      updateCharCount();
    });

    chatList.addEventListener('click', async (event) => {
      const like = event.target.closest('.chat-like');
      if (!like) {
        return;
      }
      const messageId = like.dataset.messageId;
      if (!messageId) {
        return;
      }
      const formData = new FormData();
      formData.append('action', 'chat_react');
      formData.append('message_id', messageId);
      const response = await fetch(window.aviatorConfig.playUrl, {
        method: 'POST',
        headers: {
          'X-CSRFToken': getCsrfToken(),
        },
        body: formData,
      });
      const payload = await response.json();
      if (!payload.success) {
        showSpamFilter();
        return;
      }
      const count = like.querySelector('b');
      count.textContent = String(payload.reaction_count || 0);
      like.classList.toggle('active', Boolean(payload.active));
    });

    newMessagePill?.addEventListener('click', () => {
      chatList.scrollTop = chatList.scrollHeight;
      newMessagePill.hidden = true;
    });

    document.querySelectorAll('[data-chat-emoji]').forEach((button) => {
      button.addEventListener('click', () => {
        if (!chatInput) {
          return;
        }
        chatInput.value = `${chatInput.value} 👏`.slice(0, maxChatLength);
        chatInput.focus();
        updateCharCount();
      });
    });

    renderEmojiGrid();
    document.querySelectorAll('[data-chat-emoji]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopImmediatePropagation();
        toggleEmojiPicker();
      }, true);
    });
    emojiSearch?.addEventListener('input', () => renderEmojiGrid(emojiSearch.value));
    emojiGrid?.addEventListener('click', (event) => {
      const choice = event.target.closest('.emoji-choice');
      if (!choice) {
        return;
      }
      insertEmoji(choice.textContent);
    });
    document.addEventListener('pointerdown', (event) => {
      if (emojiPicker.hidden) {
        return;
      }
      if (emojiPicker.contains(event.target) || event.target.closest('[data-chat-emoji]')) {
        return;
      }
      toggleEmojiPicker(false);
    });

    function closeChatRules() {
      rulesModal?.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('chat-rules-lock');
      chatPanel?.classList.remove('rules-open');
    }

    const rulesOpenButton = document.querySelector('[data-chat-rules-open]');
    rulesOpenButton?.addEventListener('click', () => {
      rulesModal?.setAttribute('aria-hidden', 'false');
      document.body.classList.add('chat-rules-lock');
      chatPanel?.classList.add('rules-open');
      rulesModal?.querySelector('[data-chat-rules-close]')?.focus({ preventScroll: true });
    });
    document.querySelector('[data-chat-rules-close]')?.addEventListener('click', closeChatRules);
    rulesModal?.addEventListener('click', (event) => {
      if (event.target === rulesModal) {
        closeChatRules();
      }
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !emojiPicker.hidden) {
        toggleEmojiPicker(false);
      }
      if (event.key === 'Escape' && rulesModal?.getAttribute('aria-hidden') === 'false') {
        closeChatRules();
      }
    });
    document.addEventListener('pointerdown', (event) => {
      if (!rulesModal || rulesModal.getAttribute('aria-hidden') !== 'false') {
        return;
      }
      if (rulesModal.contains(event.target) || rulesOpenButton?.contains(event.target)) {
        return;
      }
      closeChatRules();
    });
    const mobileChatMedia = window.matchMedia('(max-width: 760px)');
    let chatCloseTimer = null;

    function syncChatDrawerBackdrop() {
      chatDrawerBackdrop.hidden = !mobileChatMedia.matches || playPage?.classList.contains('chat-collapsed');
    }

    function closeChatPanel() {
      if (chatCloseTimer) {
        clearTimeout(chatCloseTimer);
      }
      playPage?.classList.add('chat-collapsed');
      reopenChatButton.hidden = false;
      chatDrawerBackdrop.hidden = true;
      chatCloseTimer = setTimeout(() => {
        playPage?.classList.add('chat-closed');
        chatPanel?.classList.add('is-collapsed');
      }, 260);
    }

    function openChatPanel() {
      if (chatCloseTimer) {
        clearTimeout(chatCloseTimer);
      }
      playPage?.classList.remove('chat-closed');
      chatPanel?.classList.remove('is-collapsed');
      requestAnimationFrame(() => {
        playPage?.classList.remove('chat-collapsed');
        syncChatDrawerBackdrop();
      });
      reopenChatButton.hidden = true;
    }

    document.querySelector('[data-chat-collapse]')?.addEventListener('click', closeChatPanel);
    chatDrawerBackdrop.addEventListener('click', closeChatPanel);
    reopenChatButton.addEventListener('click', openChatPanel);
    function syncChatMode(event) {
      if (event.matches) {
        playPage?.classList.add('chat-collapsed', 'chat-closed');
        chatPanel?.classList.add('is-collapsed');
        reopenChatButton.hidden = false;
      } else {
        playPage?.classList.remove('chat-collapsed', 'chat-closed');
        chatPanel?.classList.remove('is-collapsed');
        reopenChatButton.hidden = true;
      }
      syncChatDrawerBackdrop();
    }

    mobileChatMedia.addEventListener?.('change', syncChatMode);
    if (mobileChatMedia.matches) {
      playPage?.classList.add('chat-collapsed', 'chat-closed');
      chatPanel?.classList.add('is-collapsed');
      reopenChatButton.hidden = false;
    }
    syncChatDrawerBackdrop();

    document.querySelector('[data-chat-rain-open]')?.addEventListener('click', () => {
      rainForm.hidden = false;
      chatForm.hidden = true;
      syncRainAmounts('open');
    });
    document.querySelector('[data-chat-rain-close]')?.addEventListener('click', () => {
      rainForm.hidden = true;
      chatForm.hidden = false;
    });

    rainTotal?.addEventListener('input', () => syncRainAmounts('total'));
    rainPlayers?.addEventListener('input', () => syncRainAmounts('players'));
    rainAmount?.addEventListener('input', () => syncRainAmounts('amount'));
    rainForm?.querySelectorAll('[data-rain-step]').forEach((button) => {
      button.addEventListener('click', () => {
        const target = button.dataset.rainStep === 'players' ? rainPlayers : rainAmount;
        const step = parseFloat(button.dataset.step) || 1;
        const min = parseFloat(target.min) || 1;
        const max = parseFloat(target.max) || Infinity;
        target.value = Math.min(Math.max((parseFloat(target.value) || 0) + step, min), max).toFixed(target === rainPlayers ? 0 : 2);
        syncRainAmounts(button.dataset.rainStep === 'players' ? 'players' : 'amount');
      });
    });

    rainForm?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const submit = rainForm.querySelector('.rain-submit');
      syncRainAmounts('total');
      if (parseFloat(rainTotal.value || '0') > walletBalance() || walletBalance() < 1) {
        setRainHelper('You do not have sufficient balance on your wallet.', 'error');
        return;
      }
      submit.disabled = true;
      const response = await fetch(window.aviatorConfig.playUrl, {
        method: 'POST',
        headers: {
          'X-CSRFToken': getCsrfToken(),
        },
        body: new FormData(rainForm),
      });
      const payload = await response.json();
      submit.disabled = false;
      if (!payload.success) {
        setRainHelper(payload.message || 'Unable to send rain right now.', 'error');
        return;
      }
      renderChatMessage(payload.message, { system: true, local: true });
      const balanceDisplay = document.querySelector('#play-balance strong');
      if (balanceDisplay && typeof payload.balance === 'number') {
        balanceDisplay.textContent = payload.balance.toFixed(2);
        window.aviatorConfig.walletBalance = payload.balance;
      }
      rainForm.hidden = true;
      chatForm.hidden = false;
    });

    if (onlineCount) {
      setInterval(() => {
        const current = parseInt(onlineCount.textContent, 10) || 1093;
        onlineCount.textContent = Math.max(900, current + Math.floor(Math.random() * 7) - 3).toString();
      }, 6000);
    }
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
    let flightAnimationFrame = null;
    let flightStartedAt = 0;

    function setFlightProgress(progress, elapsedMs = 0, isFinal = false) {
      const motion = calculateFlightMotion(progress, elapsedMs, isFinal);
      movingParts.forEach((part) => {
        part.style.setProperty('--progress', progress.toFixed(4));
      });
      if (plane) {
        plane.style.setProperty('--plane-x', motion.x.toFixed(4));
        plane.style.setProperty('--plane-y', motion.y.toFixed(4));
        plane.style.setProperty('--plane-angle', motion.angle.toFixed(3));
      }
    }

    function stopFlightMotion() {
      if (flightAnimationFrame) {
        cancelAnimationFrame(flightAnimationFrame);
        flightAnimationFrame = null;
      }
    }

    function startFlightMotion() {
      stopFlightMotion();
      flightStartedAt = performance.now();
      const draw = (now) => {
        if (phase !== 'flying') {
          flightAnimationFrame = null;
          return;
        }
        const elapsedMs = now - flightStartedAt;
        const progress = calculateMaskedFlightProgress(elapsedMs / FLIGHT_TICK_MS);
        setFlightProgress(progress, elapsedMs);
        flightAnimationFrame = requestAnimationFrame(draw);
      };
      flightAnimationFrame = requestAnimationFrame(draw);
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
      stopFlightMotion();
      plane?.classList.remove('is-flew-away');
      flightStage?.classList.remove('is-crashed');
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

    async function settleCrashedRound() {
      if (!currentSession) {
        return null;
      }
      const payload = await settleRoundById(currentSession.round_id);
      if (payload && payload.fairness) {
        setFairnessText('Seed', payload.fairness.server_seed);
        return payload;
      }
      return null;
    }

    async function revealCrashedRound(attempt = 0) {
      const payload = await settleCrashedRound();
      if (payload) {
        return payload;
      }
      if (attempt < 8) {
        await new Promise((resolve) => setTimeout(resolve, 200));
        return revealCrashedRound(attempt + 1);
      }
      return null;
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
      stopFlightMotion();

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
      plane?.classList.remove('is-flew-away');
      flightStage?.classList.remove('is-crashed');
      setFlightProgress(0);
      phase = 'flying';
      startFlightMotion();
      syncSlipStates();

      gameMessage.textContent = activeSlipSessions.size ? 'Cash out before the flight crashes.' : 'Watching live round. Place a bet next round.';
      let tick = 0;
      let crashCheckInFlight = false;
      function finishCrashedRound(crashPoint, revealPromise) {
        clearInterval(flightTimer);
        stopFlightMotion();
        multiplierValue.textContent = formatMultiplier(crashPoint);
        setFlightProgress(calculateMaskedFlightProgress(tick), performance.now() - flightStartedAt, true);
        flightStage?.classList.add('is-crashed');
        requestAnimationFrame(() => plane?.classList.add('is-flew-away'));
        gameMessage.textContent = `Flew away at ${formatMultiplier(crashPoint)}. Round over.`;
        addHistoryPill(crashPoint);
        revealPromise.finally(() => {
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

      flightTimer = setInterval(() => {
        tick += 1;
        currentMultiplier = currentSession
          ? 1 + tick * FLIGHT_INCREMENT
          : Math.min(1 + tick * FLIGHT_INCREMENT, publicRound.crash_point + 0.05);
        const progress = calculateMaskedFlightProgress(tick);
        multiplierValue.textContent = formatMultiplier(currentMultiplier);
        if (!flightAnimationFrame) {
          setFlightProgress(progress, tick * FLIGHT_TICK_MS);
        }
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

        if (currentSession && !crashCheckInFlight && tick % 5 === 0) {
          crashCheckInFlight = true;
          settleCrashedRound()
            .then((payload) => {
              if (payload && phase === 'flying') {
                finishCrashedRound(payload.crash_point, Promise.resolve(payload));
              }
            })
            .finally(() => {
              crashCheckInFlight = false;
            });
        } else if (!currentSession && currentMultiplier >= publicRound.crash_point) {
          finishCrashedRound(publicRound.crash_point, Promise.resolve());
        }
      }, FLIGHT_TICK_MS);
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

    flightTimer = payload.crash_point
      ? animateFlightProgress(legacyProgressBar, multiplierValue, gameMessage, payload.crash_point, legacyCashoutButton)
      : animateServerFlightProgress(legacyProgressBar, multiplierValue, gameMessage, payload, legacyCashoutButton);
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
  let currentMultiplier = 1.0;
  let roomRunning = false;

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

  async function fetchRoomStatus() {
    const response = await fetch(window.roomConfig.roomStatusUrl, {
      headers: {
        'X-CSRFToken': getCsrfToken(),
      },
    });
    const payload = await response.json();
    return payload.success ? payload : null;
  }

  function finishRoomFlight(payload) {
    roomRunning = false;
    if (roomTimer) {
      clearInterval(roomTimer);
    }
    if (payload?.room?.crash_point) {
      roomMultiplier.textContent = formatMultiplier(payload.room.crash_point);
      roomMessage.textContent = `Flew away at ${formatMultiplier(payload.room.crash_point)}. Game ended.`;
    } else {
      roomMessage.textContent = 'Game ended. Review the results below.';
    }
    if (cashoutButton) {
      cashoutButton.disabled = true;
    }
  }

  function startRoomFlight() {
    let tick = 0;
    let statusCheckInFlight = false;

    roomRunning = true;
    roomGamePanel.classList.remove('hidden');
    if (cashoutButton) {
      cashoutButton.disabled = false;
    }
    roomMessage.textContent = 'Game in progress. Cash out before the crash.';
    if (roomTimer) {
      clearInterval(roomTimer);
    }

    roomTimer = setInterval(() => {
      tick += 1;
      currentMultiplier = 1 + tick * FLIGHT_INCREMENT;
      roomMultiplier.textContent = formatMultiplier(currentMultiplier);
      roomProgress.style.width = `${calculateMaskedFlightProgress(tick)}%`;

      if (!statusCheckInFlight && tick % 5 === 0) {
        statusCheckInFlight = true;
        fetchRoomStatus()
          .then((payload) => {
            if (!payload) {
              return;
            }
            updateParticipants(payload);
            if (payload.room.status === 'ended') {
              finishRoomFlight(payload);
            }
          })
          .finally(() => {
            statusCheckInFlight = false;
          });
      }
    }, FLIGHT_TICK_MS);
  }

  async function pollRoomStatus() {
    const payload = await fetchRoomStatus();
    if (!payload) {
      return;
    }

    updateParticipants(payload);

    if (payload.room.status === 'running' && !roomRunning) {
      startRoomFlight();
    }

    if (payload.room.status === 'ended' && roomRunning) {
      finishRoomFlight(payload);
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

      startRoomFlight();
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

  if (backdrop.parentElement !== document.body) {
    document.body.append(backdrop);
  }
  modals.forEach((modal) => {
    if (modal.parentElement !== document.body) {
      document.body.append(modal);
    }
  });

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
