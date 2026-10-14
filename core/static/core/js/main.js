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

// Match the server's measured 0.10x-per-second round pace. At this rate 1.70x
// takes roughly seven seconds, giving players a usable cash-out window.
const FLIGHT_INCREMENT = 0.01;
const FLIGHT_TICK_MS = 100;

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
  return payload;
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
          if (payload?.success) {
            clearInterval(interval);
            multiplierElement.textContent = formatMultiplier(payload.crash_point);
            cashoutButton.disabled = true;
            messageElement.textContent = 'Flight crashed. Round over.';
          } else if (Number.isFinite(payload?.server_multiplier)) {
            multiplierElement.textContent = formatMultiplier(payload.server_multiplier);
          }
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

  if (window.aviatorConfig?.isAuthenticated) {
    const presenceKey = 'xxxxxxxxxxxx4xxxyxxxxxxxxxxxxxxx'.replace(/[xy]/g, (character) => {
      const value = Math.floor(Math.random() * 16);
      return (character === 'x' ? value : (value & 0x3) | 0x8).toString(16);
    });
    const sendPresence = () => {
      if (document.visibilityState !== 'visible') {
        releasePresence();
        return;
      }
      const formData = new FormData();
      formData.append('action', 'presence');
      formData.append('presence_key', presenceKey);
      fetch(window.aviatorConfig.playUrl, {
        method: 'POST',
        headers: { 'X-CSRFToken': getCsrfToken() },
        body: formData,
      }).then((response) => {
        if (!response.ok) {
          throw new Error(`Game presence update failed (${response.status}).`);
        }
      }).catch((error) => {
        console.error('Unable to update game presence.', error);
      });
    };
    const releasePresence = () => {
      const formData = new FormData();
      formData.append('action', 'presence_leave');
      formData.append('presence_key', presenceKey);
      fetch(window.aviatorConfig.playUrl, {
        method: 'POST',
        headers: { 'X-CSRFToken': getCsrfToken() },
        body: formData,
        keepalive: true,
      }).catch((error) => {
        console.error('Unable to release game presence.', error);
      });
    };
    sendPresence();
    const presenceInterval = setInterval(sendPresence, 30000);
    document.addEventListener('visibilitychange', sendPresence);
    window.addEventListener('pagehide', () => {
      clearInterval(presenceInterval);
      releasePresence();
    }, { once: true });
  }

  if (chatList) {
    const chatPanel = document.querySelector('[data-chat-panel]');
    const chatInput = chatForm?.querySelector('input[name="message"]');
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
    const warningModal = document.getElementById('chat-warning-modal');
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
        <span aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="10.8" cy="10.8" r="6.8"></circle><path d="m16 16 4.3 4.3"></path></svg></span>
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
      if (message.textContent.includes('W H A T')) {
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
      if (shouldOpen && rainForm && !rainForm.hidden) {
        rainForm.hidden = true;
        chatPanel?.classList.remove('rain-open');
      }
      emojiPicker.hidden = !shouldOpen;
      chatPanel?.classList.toggle('emoji-open', shouldOpen);
      if (shouldOpen) {
        renderEmojiGrid(emojiSearch?.value || '');
        emojiSearch?.focus({ preventScroll: true });
      }
    }

    function closeRainPanel() {
      if (!rainForm || rainForm.hidden) {
        return;
      }
      rainForm.hidden = true;
      chatPanel?.classList.remove('rain-open');
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

    function showChatWarning(message) {
      const warningMessage = document.getElementById('chat-warning-message');
      if (!warningModal || !warningMessage) {
        return;
      }
      warningMessage.textContent = message;
      if (!warningModal.open) {
        warningModal.showModal();
      }
      warningModal.querySelector('[data-chat-warning-close]')?.focus({ preventScroll: true });
    }

    function closeChatWarning() {
      if (warningModal?.open) {
        warningModal.close();
      }
      chatInput?.focus({ preventScroll: true });
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
      rainHelper.textContent = message || 'Randomly distributes to players currently in the game.';
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

    chatForm?.addEventListener('submit', async (event) => {
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
        if (payload.message) {
          showChatWarning(payload.message);
          return;
        }
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
      const target = event.target;
      if (!(target instanceof Element)) {
        return;
      }
      if (!emojiPicker.hidden && !emojiPicker.contains(target) && !target.closest('[data-chat-emoji]')) {
        toggleEmojiPicker(false);
      }
      if (rainForm && !rainForm.hidden && !rainForm.contains(target) && !target.closest('[data-chat-rain-open]')) {
        closeRainPanel();
      }
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
    warningModal?.querySelectorAll('[data-chat-warning-close]').forEach((button) => {
      button.addEventListener('click', closeChatWarning);
    });
    warningModal?.addEventListener('click', (event) => {
      if (event.target === warningModal) {
        closeChatWarning();
      }
    });
    warningModal?.addEventListener('close', () => {
      chatInput?.focus({ preventScroll: true });
    });
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
    const mobileChatMedia = window.matchMedia('(max-width: 1120px)');
    let chatCloseTimer = null;

    function syncChatDrawerBackdrop() {
      chatDrawerBackdrop.hidden = !mobileChatMedia.matches || playPage?.classList.contains('chat-collapsed');
    }

    function closeChatPanel() {
      if (chatCloseTimer) {
        clearTimeout(chatCloseTimer);
      }
      playPage?.classList.remove('chat-open');
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
      playPage?.classList.add('chat-open');
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
      playPage?.classList.remove('chat-open');
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
      if (rainForm && !rainForm.hidden) {
        closeRainPanel();
        return;
      }
      toggleEmojiPicker(false);
      rainForm.hidden = false;
      chatPanel?.classList.add('rain-open');
      syncRainAmounts('open');
    });
    document.querySelector('[data-chat-rain-close]')?.addEventListener('click', () => {
      closeRainPanel();
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
      chatPanel?.classList.remove('rain-open');
    });

    if (onlineCount) {
      const refreshOnlineCount = async () => {
        try {
          const response = await fetch(onlineCount.dataset.url, {
            headers: { Accept: 'application/json' },
            cache: 'no-store',
          });
          if (!response.ok) {
            throw new Error(`Online count request failed (${response.status}).`);
          }
          const payload = await response.json();
          if (Number.isInteger(payload.online_count) && payload.online_count >= 0) {
            onlineCount.textContent = String(payload.online_count);
          }
        } catch (error) {
          console.error('Unable to refresh online player count.', error);
        }
      };
      refreshOnlineCount();
      setInterval(refreshOnlineCount, 15000);
    }
  }

  const professionalWidget = playForm.hasAttribute('data-play-widget');
  if (professionalWidget) {
    const slips = [...playForm.querySelectorAll('.aviator-bet-slip')];
    const startButtons = [...playForm.querySelectorAll('.start-bet')];
    const cashoutButtons = [...playForm.querySelectorAll('.cashout-bet')];
    const fairnessStrip = document.getElementById('fairness-strip');
    const flightStage = document.querySelector('.flight-stage');
    const countdown = document.getElementById('round-countdown');
    const roundStrip = document.querySelector('.round-strip');
    const roundHistoryList = document.getElementById('round-history-list');
    const roundHistoryToggle = document.querySelector('.round-history-toggle');
    const ledgerList = document.getElementById('ledger-list');
    const totalBetsCount = document.getElementById('total-bets-count');
    const ledgerPageStatus = document.getElementById('ledger-page-status');
    const ledgerPrevious = document.getElementById('ledger-previous');
    const ledgerNext = document.getElementById('ledger-next');
    const ledgerTabs = [...document.querySelectorAll('[data-ledger-tab]')];
    const ledgerPanel = document.getElementById('ledger-list');
    const currency = window.aviatorConfig.currency || 'UGX';
    let currentMultiplier = 1.0;
    let queuedSlips = new Set();
    let activeSlipSessions = new Map();
    let betPlacementPending = new Set();
    let placedBetRounds = new Map();
    let betRequestKeys = new Map();
    let settledBets = new Map();
    let insufficientBets = new Set();
    let autoRepeatSlips = new Set();
    let autoRepeatStoppedSlips = new Set();
    let cashedOutSlips = new Set();
    let pendingCashoutSlips = new Set();
    let autoSlips = new Set();
    let phase = 'betting';
    let currentLedgerTab = 'all';
    let ledgerRows = [];
    let ledgerRoundId = null;
    let ledgerPage = 1;
    let ledgerTotalCount = 0;
    let ledgerTabCount = 0;
    let ledgerCursor = null;
    let ledgerPreviousCursor = null;
    let ledgerNextCursor = null;
    let ledgerHasPrevious = false;
    let ledgerHasNext = false;
    let ledgerRequestId = 0;
    let ledgerLoading = false;
    let ledgerRefreshQueued = false;
    let flightAnimationFrame = null;
    let flightStartedAt = 0;
    let finishCurrentFlight = null;
    let lastHandledCrashRoundId = null;
    let bettingDeadlineMonotonic = 0;
    let bettingOpen = false;

    if (roundStrip && roundHistoryToggle) {
      const setRoundHistoryExpanded = (expanded) => {
        roundStrip.classList.toggle('is-expanded', expanded);
        roundHistoryToggle.setAttribute('aria-expanded', String(expanded));
        roundHistoryToggle.setAttribute('aria-label', `${expanded ? 'Collapse' : 'Expand'} odds history`);
      };

      roundHistoryToggle.addEventListener('click', () => {
        setRoundHistoryExpanded(roundHistoryToggle.getAttribute('aria-expanded') !== 'true');
      });

      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && roundHistoryToggle.getAttribute('aria-expanded') === 'true') {
          setRoundHistoryExpanded(false);
          roundHistoryToggle.focus();
        }
      });
    }

    function drawFlightTrail(motion) {
      if (!flightCurve || !flightFill) {
        return;
      }
      const x = Math.min(Math.max(motion.x, 0), 94);
      const y = Math.min(Math.max(motion.y, 0), 78);
      const endY = 100 - y;
      const curve = `M 0 100 C ${(x * 0.28).toFixed(3)} 100, ${(x * 0.7).toFixed(3)} ${(100 - (y * 0.38)).toFixed(3)}, ${x.toFixed(3)} ${endY.toFixed(3)}`;
      flightFill.setAttribute('d', `${curve} L ${x.toFixed(3)} 100 Z`);
      flightCurve.querySelector('#flight-line')?.setAttribute('d', curve);
    }

    function setFlightProgress(progress, elapsedMs = 0, isFinal = false) {
      const motion = calculateFlightMotion(progress, elapsedMs, isFinal);
      drawFlightTrail(motion);
      if (plane) {
        plane.style.setProperty('--plane-x', `${motion.x.toFixed(4)}%`);
        plane.style.setProperty('--plane-y', `${motion.y.toFixed(4)}%`);
        plane.style.setProperty('--plane-angle', `${(-motion.angle).toFixed(3)}deg`);
      }
    }

    function stopFlightMotion() {
      if (flightAnimationFrame) {
        cancelAnimationFrame(flightAnimationFrame);
        flightAnimationFrame = null;
      }
    }

    function startFlightMotion(elapsedOffset = 0) {
      stopFlightMotion();
      flightStartedAt = performance.now() - elapsedOffset;
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
        const startLabel = slip.querySelector('.start-bet span');
        const start = slip.querySelector('.start-bet strong');
        const cashout = slip.querySelector('.cashout-bet strong');
        const stake = Math.max(parseFloat(input.value) || 0, 0);
        const placedThisRound = placedBetRounds.get(slip) === currentSession?.round_id;
        const settled = settledBets.get(slip);
        const settledWon = Boolean(settled && settled.won_amount > 0);
        if (start) {
          start.textContent = betPlacementPending.has(slip)
            ? 'PLACING BET...'
            : settled
              ? `${settled.won_amount.toFixed(2)} ${currency}`
              : queuedSlips.has(slip)
                ? 'NEXT ROUND'
              : placedThisRound
                ? 'BET PLACED'
                : `${stake.toFixed(2)} ${currency}`;
        }
        if (startLabel) {
          startLabel.textContent = insufficientBets.has(slip)
            ? 'AMOUNT NOT ENOUGH'
            : settled
              ? settledWon ? 'CASHED OUT' : 'LOST'
            : queuedSlips.has(slip)
              ? 'CANCEL'
              : 'BET';
        }
        if (cashout) {
          cashout.textContent = settled
            ? `${settled.won_amount.toFixed(2)} ${currency}`
            : `${(stake * currentMultiplier).toFixed(2)} ${currency}`;
        }
        slip.classList.toggle('is-queued', queuedSlips.has(slip));
        slip.classList.toggle(
          'is-running',
          phase === 'flying'
            && activeSlipSessions.has(slip)
            && !cashedOutSlips.has(slip),
        );
        slip.classList.toggle('is-auto', autoSlips.has(slip));
        slip.classList.toggle('is-settled-won', settledWon);
        slip.classList.toggle('is-settled-lost', Boolean(settled && !settledWon));
        slip.classList.toggle('has-insufficient-balance', insufficientBets.has(slip));
      });
    }

    function setWalletBalance(balance) {
      if (!Number.isFinite(balance)) {
        return;
      }
      window.aviatorConfig.walletBalance = balance;
      const balanceDisplay = document.querySelector('#play-balance strong');
      if (balanceDisplay) {
        balanceDisplay.textContent = balance.toFixed(2);
      }
      insufficientBets.forEach((slip) => {
        const amount = Number(slip.querySelector('.stake-input').value);
        if (Number.isFinite(amount) && amount <= balance) {
          insufficientBets.delete(slip);
        }
      });
    }

    function clearInsufficientIfAffordable(slip) {
      if (
        insufficientBets.has(slip)
        && Number(slip.querySelector('.stake-input').value) <= walletBalance()
      ) {
        insufficientBets.delete(slip);
      }
    }

    function syncSlipStates() {
      cashoutButtons.forEach((button) => {
        const slip = button.closest('.aviator-bet-slip');
        button.disabled = !activeSlipSessions.has(slip) || cashedOutSlips.has(slip) || pendingCashoutSlips.has(slip) || phase !== 'flying';
      });
      startButtons.forEach((button) => {
        const slip = button.closest('.aviator-bet-slip');
        button.disabled = (
          (phase !== 'betting' && phase !== 'flying')
          || (phase === 'betting' && !bettingOpen)
          || betPlacementPending.has(slip)
          || insufficientBets.has(slip)
          || (
            phase === 'betting'
            && placedBetRounds.get(slip) === currentSession?.round_id
            && !queuedSlips.has(slip)
          )
        );
      });
      syncBetLabels();
    }

    function resetRound() {
      currentSession = null;
      currentMultiplier = 1.0;
      activeSlipSessions.clear();
      settledBets.clear();
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

    function addHistoryPill(crashPoint) {
      if (!roundHistoryList) {
        return;
      }
      const pill = document.createElement('span');
      pill.className = `round-pill ${crashPoint >= 10 ? 'hot' : crashPoint >= 2 ? 'mid' : ''}`;
      pill.textContent = formatMultiplier(crashPoint);
      roundHistoryList.prepend(pill);
      [...roundHistoryList.querySelectorAll('.round-pill')].slice(50).forEach((item) => item.remove());
    }

    const ledgerMoneyFormatter = new Intl.NumberFormat(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });

    function rowTemplate(row) {
      return `
        <div class="ledger-row ${row.status === 'won' ? 'won' : 'muted'}">
          <span class="ledger-user"><span class="player-avatar">${escapeHtml(row.avatar || '')}</span>${escapeHtml(row.user || '')}</span>
          <span>${ledgerMoneyFormatter.format(row.bet)}</span>
          <span>${Number.isFinite(row.multiplier) ? formatMultiplier(row.multiplier) : ''}</span>
          <span>${row.win ? ledgerMoneyFormatter.format(row.win) : ''}</span>
        </div>
      `;
    }

    function renderLedger() {
      if (!ledgerList) {
        return;
      }
      const emptyMessage = currentLedgerTab === 'top'
        ? 'No winners in this round yet.'
        : currentLedgerTab === 'mine' && window.aviatorConfig?.isAuthenticated
          ? 'You have not placed a bet in this round.'
          : currentLedgerTab === 'mine'
            ? 'Log in to view your bets.'
            : 'No players have placed bets in this round yet.';
      ledgerList.innerHTML = ledgerRows.length
        ? ledgerRows.map(rowTemplate).join('')
        : `<div class="ledger-empty">${emptyMessage}</div>`;
      if (totalBetsCount) {
        totalBetsCount.textContent = ledgerTotalCount.toLocaleString();
      }
      if (ledgerPageStatus) {
        ledgerPageStatus.textContent = `Page ${ledgerPage} · ${ledgerTabCount.toLocaleString()} bets`;
      }
      if (ledgerPrevious) {
        ledgerPrevious.disabled = !ledgerHasPrevious;
      }
      if (ledgerNext) {
        ledgerNext.disabled = !ledgerHasNext;
      }
      ledgerPanel?.setAttribute(
        'aria-labelledby',
        `ledger-tab-${currentLedgerTab}`,
      );
    }

    async function loadLedger() {
      if (document.visibilityState !== 'visible') {
        return;
      }
      if (ledgerLoading) {
        ledgerRefreshQueued = true;
        return;
      }
      ledgerLoading = true;
      ledgerRefreshQueued = false;
      const requestId = ++ledgerRequestId;
      const requestedTab = currentLedgerTab;
      const requestedPage = ledgerPage;
      const requestedCursor = ledgerCursor;
      const formData = new FormData();
      formData.append('action', 'ledger');
      formData.append('tab', requestedTab);
      if (requestedCursor) {
        formData.append('cursor', requestedCursor);
      }

      try {
        const response = await fetch(window.aviatorConfig.playUrl, {
          method: 'POST',
          headers: { 'X-CSRFToken': getCsrfToken() },
          body: formData,
        });
        if (response.status === 409) {
          ledgerCursor = null;
          ledgerPreviousCursor = null;
          ledgerNextCursor = null;
          ledgerPage = 1;
          ledgerRefreshQueued = true;
          return;
        }
        if (!response.ok) {
          throw new Error(`Live bet list request failed (${response.status}).`);
        }
        const payload = await response.json();
        if (
          requestId !== ledgerRequestId
          || requestedTab !== currentLedgerTab
          || requestedPage !== ledgerPage
          || requestedCursor !== ledgerCursor
        ) {
          return;
        }
        if (!payload.success) {
          throw new Error(payload.message || 'Unable to load current-round bets.');
        }
        if (ledgerRoundId !== payload.round_id) {
          ledgerRoundId = payload.round_id;
          ledgerPage = 1;
          ledgerCursor = null;
          ledgerPreviousCursor = null;
          ledgerNextCursor = null;
          if (requestedPage !== 1) {
            ledgerRefreshQueued = true;
            return;
          }
        }
        ledgerTotalCount = payload.total_count;
        ledgerTabCount = payload.tab_count;
        ledgerRows = payload.rows;
        ledgerHasPrevious = payload.has_previous;
        ledgerHasNext = payload.has_next;
        ledgerPreviousCursor = payload.previous_cursor;
        ledgerNextCursor = payload.next_cursor;
        renderLedger();
      } catch (error) {
        console.error('Unable to refresh live bets.', error);
      } finally {
        ledgerLoading = false;
        if (ledgerRefreshQueued) {
          loadLedger();
        }
      }
    }

    ledgerTabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        currentLedgerTab = tab.dataset.ledgerTab || 'all';
        ledgerRequestId += 1;
        ledgerTabs.forEach((item) => {
          const selected = item === tab;
          item.classList.toggle('active', selected);
          item.setAttribute('aria-selected', String(selected));
          item.tabIndex = selected ? 0 : -1;
        });
        ledgerRows = [];
        ledgerPage = 1;
        ledgerCursor = null;
        ledgerPreviousCursor = null;
        ledgerNextCursor = null;
        ledgerHasPrevious = false;
        ledgerHasNext = false;
        renderLedger();
        loadLedger();
      });
      tab.addEventListener('keydown', (event) => {
        const currentIndex = ledgerTabs.indexOf(tab);
        let nextIndex = currentIndex;
        if (event.key === 'ArrowRight') {
          nextIndex = (currentIndex + 1) % ledgerTabs.length;
        } else if (event.key === 'ArrowLeft') {
          nextIndex = (currentIndex - 1 + ledgerTabs.length) % ledgerTabs.length;
        } else if (event.key === 'Home') {
          nextIndex = 0;
        } else if (event.key === 'End') {
          nextIndex = ledgerTabs.length - 1;
        } else {
          return;
        }
        event.preventDefault();
        ledgerTabs[nextIndex].focus();
        ledgerTabs[nextIndex].click();
      });
    });

    async function settleCrashedRound() {
      if (!currentSession) {
        return null;
      }
      const payload = await settleRoundById(currentSession.round_id);
      if (payload && payload.fairness) {
        setFairnessText('Seed', payload.fairness.server_seed);
        const seedStatus = document.querySelector('[data-server-seed-status]');
        if (seedStatus) {
          seedStatus.textContent = 'The server seed has been revealed. Verify it against this commitment in your round history.';
        }
      }
      return payload;
    }

    async function revealCrashedRound(attempt = 0) {
      const payload = await settleCrashedRound();
      if (payload?.success) {
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
            autoRepeatStoppedSlips.delete(slip);
            if (phase === 'flying' || activeSlipSessions.has(slip)) {
              autoRepeatSlips.add(slip);
              if (activeSlipSessions.has(slip)) {
                queuedSlips.delete(slip);
                gameMessage.textContent = 'Auto bet enabled for the next round. Current bet remains active.';
              } else {
                queuedSlips.add(slip);
                gameMessage.textContent = 'Auto bet queued for the next round.';
              }
            } else {
              gameMessage.textContent = 'Auto bet enabled for this panel.';
            }
          } else {
            autoSlips.delete(slip);
            autoRepeatSlips.delete(slip);
            autoRepeatStoppedSlips.add(slip);
            queuedSlips.delete(slip);
            gameMessage.textContent = 'Manual bet mode enabled.';
          }
          modeButtons.forEach((item) => item.classList.toggle('active', item === button));
          syncSlipStates();
        });
      });
      slip.querySelectorAll('[data-stake]').forEach((button) => {
        button.addEventListener('click', () => {
          input.value = parseFloat(button.dataset.stake).toFixed(2);
          clearInsufficientIfAffordable(slip);
          syncSlipStates();
        });
      });
      const minus = slip.querySelector('.amount-minus');
      const plus = slip.querySelector('.amount-plus');
      minus.addEventListener('click', () => {
        input.value = Math.max(1, (parseFloat(input.value) || 1) - 1).toFixed(2);
        clearInsufficientIfAffordable(slip);
        syncSlipStates();
      });
      plus.addEventListener('click', () => {
        input.value = ((parseFloat(input.value) || 0) + 1).toFixed(2);
        clearInsufficientIfAffordable(slip);
        syncSlipStates();
      });
      input.addEventListener('input', () => {
        clearInsufficientIfAffordable(slip);
        syncSlipStates();
      });
    });

    function createBetRequestId() {
      const bytes = new Uint8Array(16);
      window.crypto.getRandomValues(bytes);
      return [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('');
    }

    async function placeBetForSlip(slip, roundId) {
      if (
        !window.aviatorConfig?.isAuthenticated
        || phase !== 'betting'
        || currentSession?.round_id !== roundId
        || betPlacementPending.has(slip)
        || placedBetRounds.get(slip) === roundId
      ) {
        return;
      }
      const amount = Number(slip.querySelector('.stake-input').value);
      if (!Number.isFinite(amount) || amount <= 0) {
        gameMessage.textContent = 'Enter a valid bet amount.';
        return;
      }

      let request = betRequestKeys.get(slip);
      if (!request || request.roundId !== roundId) {
        request = { roundId, id: createBetRequestId() };
        betRequestKeys.set(slip, request);
      }
      const autoCashoutInput = slip.querySelector('.auto-cashout-input');
      const autoCashoutAt = autoSlips.has(slip)
        ? Number(autoCashoutInput?.value)
        : null;
      if (autoSlips.has(slip) && (!Number.isFinite(autoCashoutAt) || autoCashoutAt < 1.01)) {
        gameMessage.textContent = 'Enter an auto cashout target of at least 1.01x.';
        return;
      }

      betPlacementPending.add(slip);
      syncSlipStates();
      const formData = new FormData();
      formData.append('action', 'start');
      formData.append('bet_data', JSON.stringify([{
        amount,
        auto_cashout_at: autoCashoutAt,
        request_id: request.id,
      }]));
      try {
        const response = await fetch(window.aviatorConfig.playUrl, {
          method: 'POST',
          headers: { 'X-CSRFToken': getCsrfToken() },
          body: formData,
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) {
          if (response.status === 409 && payload.code === 'betting_closed') {
            queuedSlips.add(slip);
            if (autoSlips.has(slip)) {
              autoRepeatSlips.add(slip);
            }
            gameMessage.textContent = 'Betting closed before your bet was accepted. Bet queued for the next round.';
          } else if (payload.code === 'insufficient_balance') {
            insufficientBets.add(slip);
            autoRepeatSlips.delete(slip);
            autoRepeatStoppedSlips.add(slip);
            queuedSlips.delete(slip);
            if (Number.isFinite(payload.balance)) {
              setWalletBalance(payload.balance);
            }
            gameMessage.textContent = 'Insufficient balance for this bet. Lower the stake or add funds.';
          } else {
            gameMessage.textContent = payload.message || 'Bet was not accepted. Your balance was not changed.';
            queuedSlips.delete(slip);
            autoRepeatSlips.delete(slip);
          }
          return;
        }
        if (payload.round_id !== roundId || payload.status !== 'betting') {
          queuedSlips.add(slip);
          gameMessage.textContent = 'Round changed before the bet was confirmed. Bet queued for the next round.';
          return;
        }
        const placedSession = payload.sessions?.find((session) => session.bet_request_id === request.id);
        if (!placedSession) {
          throw new Error('The server accepted the bet but did not return its bet record.');
        }
        activeSlipSessions.set(slip, placedSession.id);
        placedBetRounds.set(slip, roundId);
        betRequestKeys.delete(slip);
        if (autoSlips.has(slip)) {
          autoRepeatSlips.add(slip);
          autoRepeatStoppedSlips.delete(slip);
          queuedSlips.delete(slip);
        } else {
          queuedSlips.delete(slip);
        }
        if (Number.isFinite(payload.balance)) {
          setWalletBalance(payload.balance);
        }
        gameMessage.textContent = `Bet accepted for round ${roundId}.`;
        loadLedger();
      } catch (error) {
        console.error('Unable to confirm bet placement.', error);
        queuedSlips.add(slip);
        gameMessage.textContent = 'Bet confirmation is delayed. The same request will be retried safely.';
      } finally {
        betPlacementPending.delete(slip);
        syncSlipStates();
      }
    }

    function queueBet(slip) {
      if (phase !== 'betting' && phase !== 'flying') {
        gameMessage.textContent = 'Wait for the next betting window.';
        return;
      }
      if (phase === 'betting') {
        if (activeSlipSessions.has(slip)) {
          if (autoSlips.has(slip)) {
            if (queuedSlips.has(slip)) {
              queuedSlips.delete(slip);
              autoRepeatSlips.delete(slip);
              autoRepeatStoppedSlips.add(slip);
              gameMessage.textContent = 'Next-round auto bet cancelled.';
            } else {
              queuedSlips.add(slip);
              autoRepeatSlips.add(slip);
              autoRepeatStoppedSlips.delete(slip);
              gameMessage.textContent = 'Auto bet queued for the next round.';
            }
            syncSlipStates();
          }
          return;
        }
        placeBetForSlip(slip, currentSession.round_id);
        return;
      }
      if (queuedSlips.has(slip)) {
        queuedSlips.delete(slip);
        autoRepeatSlips.delete(slip);
        if (autoSlips.has(slip)) {
          autoRepeatStoppedSlips.add(slip);
        }
        gameMessage.textContent = 'Bet removed from the next round.';
        syncSlipStates();
        return;
      }
      if (activeSlipSessions.has(slip) && !autoSlips.has(slip)) {
        return;
      }
      queuedSlips.add(slip);
      if (autoSlips.has(slip)) {
        autoRepeatSlips.add(slip);
        autoRepeatStoppedSlips.delete(slip);
      }
      gameMessage.textContent = phase === 'flying'
        ? autoSlips.has(slip)
          ? 'Auto bet queued for the next round.'
          : 'Bet accepted for the next round.'
        : 'Bet accepted for the next flight.';
      syncSlipStates();
    }

    function activateFlight(payload, enteredSlips = [], consumeQueued = false, elapsedOffset = null) {
      if (flightTimer) {
        clearInterval(flightTimer);
      }
      if (bettingTimer) {
        clearInterval(bettingTimer);
        bettingTimer = null;
      }
      flightStage?.classList.remove('is-betting');
      if (countdown) {
        countdown.style.display = 'none';
      }
      stopFlightMotion();
      currentSession = payload;
      bettingOpen = false;
      activeSlipSessions.clear();
      settledBets.clear();
      const sessions = payload.sessions || (payload.session_ids || []).map((id) => ({ id }));
      sessions.forEach((session, index) => {
        const slip = enteredSlips[index] || slips[index];
        if (!slip) {
          return;
        }
        const autoCashoutInput = slip.querySelector('.auto-cashout-input');
        if (Number.isFinite(session.auto_cashout_at) && autoCashoutInput) {
          autoCashoutInput.value = session.auto_cashout_at.toFixed(2);
          autoSlips.add(slip);
          if (!autoRepeatStoppedSlips.has(slip)) {
            autoRepeatSlips.add(slip);
          }
        }
        if (session.cashed_out_at == null) {
          return;
        }
        settledBets.set(slip, {
          cashed_out_at: session.cashed_out_at,
          won_amount: session.won_amount || 0,
        });
        cashedOutSlips.add(slip);
      });
      sessions
        .filter((session) => session.cashed_out_at == null)
        .slice(0, slips.length)
        .forEach((session, index) => {
          const slip = enteredSlips[index] || slips[index];
          if (!slip) {
            return;
          }
          activeSlipSessions.set(slip, session.id);
          placedBetRounds.set(slip, payload.round_id);
          if (Number.isFinite(session.stake)) {
            slip.querySelector('.stake-input').value = session.stake.toFixed(2);
          }
        });
      loadLedger();
      if (payload.server_seed_hash) {
        setFairnessText('Hash', payload.server_seed_hash);
        const committedHash = document.querySelector('[data-server-seed-hash]');
        if (committedHash) {
          committedHash.textContent = payload.server_seed_hash;
        }
      }
      const seedStatus = document.querySelector('[data-server-seed-status]');
      if (seedStatus && payload.server_seed_hash) {
        seedStatus.textContent = 'Commitment published before this round began.';
      }
      const balanceDisplay = document.querySelector('#play-balance strong');
      if (balanceDisplay && Number.isFinite(payload.balance)) {
        balanceDisplay.textContent = payload.balance.toFixed(2);
      }

      if (consumeQueued) {
        queuedSlips.clear();
      }
      const initialElapsed = Number.isFinite(elapsedOffset)
        ? elapsedOffset
        : Number.isFinite(payload.current_multiplier)
          ? ((payload.current_multiplier - 1) / FLIGHT_INCREMENT) * FLIGHT_TICK_MS
          : 0;
      currentMultiplier = 1 + Math.round(initialElapsed / FLIGHT_TICK_MS) * FLIGHT_INCREMENT;
      cashedOutSlips.clear();
      multiplierValue.textContent = formatMultiplier(currentMultiplier);
      plane?.classList.remove('is-flew-away');
      flightStage?.classList.remove('is-crashed');
      let tick = Math.max(0, Math.round(initialElapsed / FLIGHT_TICK_MS));
      setFlightProgress(calculateMaskedFlightProgress(tick), initialElapsed);
      phase = 'flying';
      startFlightMotion(initialElapsed);
      syncSlipStates();

      gameMessage.textContent = activeSlipSessions.size ? 'Cash out before the flight crashes.' : 'Watching live round. Place a bet next round.';
      let crashCheckInFlight = false;
      let crashFinished = false;
      function finishCrashedRound(crashPoint, revealPromise) {
        if (crashFinished) {
          return;
        }
        crashFinished = true;
        clearInterval(flightTimer);
        stopFlightMotion();
        multiplierValue.textContent = formatMultiplier(crashPoint);
        setFlightProgress(calculateMaskedFlightProgress(tick), performance.now() - flightStartedAt, true);
        flightStage?.classList.add('is-crashed');
        requestAnimationFrame(() => plane?.classList.add('is-flew-away'));
        gameMessage.textContent = `Flew away at ${formatMultiplier(crashPoint)}. Round over.`;
        addHistoryPill(crashPoint);
        revealPromise.finally(() => {
          loadLedger();
          phase = 'crashed';
          lastHandledCrashRoundId = currentSession?.round_id || lastHandledCrashRoundId;
          currentSession = null;
          activeSlipSessions.clear();
          cashedOutSlips.clear();
          pendingCashoutSlips.clear();
          syncSlipStates();
        });
      }
      finishCurrentFlight = finishCrashedRound;

      flightTimer = setInterval(() => {
        tick = Math.max(0, Math.round((performance.now() - flightStartedAt) / FLIGHT_TICK_MS));
        currentMultiplier = 1 + tick * FLIGHT_INCREMENT;
        const progress = calculateMaskedFlightProgress(tick);
        multiplierValue.textContent = formatMultiplier(currentMultiplier);
        if (!flightAnimationFrame) {
          setFlightProgress(progress, tick * FLIGHT_TICK_MS);
        }
        syncBetLabels();
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
        if (currentSession && !crashCheckInFlight && tick % 5 === 0) {
          crashCheckInFlight = true;
          settleCrashedRound()
            .then((payload) => {
              if (payload?.success && phase === 'flying') {
                finishCrashedRound(payload.crash_point, Promise.resolve(payload));
              } else if (Number.isFinite(payload?.server_multiplier) && phase === 'flying') {
                currentMultiplier = payload.server_multiplier;
                multiplierValue.textContent = formatMultiplier(currentMultiplier);
              }
            })
            .finally(() => {
              crashCheckInFlight = false;
            });
        }
      }, FLIGHT_TICK_MS);
    }

    function placeQueuedBets(roundId) {
      if (!bettingOpen || phase !== 'betting') {
        return;
      }
      autoRepeatSlips.forEach((slip) => {
        if (placedBetRounds.get(slip) !== roundId) {
          queuedSlips.add(slip);
        }
      });
      [...queuedSlips].forEach((slip) => placeBetForSlip(slip, roundId));
    }

    function startBettingWindow(payload, synchronizedDeadline = null) {
      if (!payload?.round_id || !Number.isFinite(payload.seconds_remaining)) {
        throw new Error('The server did not provide a valid shared betting window.');
      }
      if (bettingTimer) {
        clearInterval(bettingTimer);
        bettingTimer = null;
      }
      phase = 'betting';
      resetRound();
      currentSession = { round_id: payload.round_id };
      bettingDeadlineMonotonic = Number.isFinite(synchronizedDeadline)
        ? synchronizedDeadline
        : performance.now() + (payload.seconds_remaining * 1000);
      const remainingMs = Math.max(bettingDeadlineMonotonic - performance.now(), 0);
      bettingOpen = remainingMs > 0;
      if (flightStage) {
        flightStage.classList.add('is-betting');
      }
      if (countdown) {
        countdown.style.display = 'grid';
        countdown.querySelector('strong').textContent = Math.ceil(remainingMs / 1000);
      }
      let secondsLeft = Math.ceil(remainingMs / 1000);
      gameMessage.textContent = 'Place your bets for the next round.';
      placeQueuedBets(payload.round_id);
      bettingTimer = setInterval(() => {
        secondsLeft = Math.max(0, Math.ceil((bettingDeadlineMonotonic - performance.now()) / 1000));
        if (countdown) {
          countdown.querySelector('strong').textContent = secondsLeft;
        }
        if (secondsLeft <= 0) {
          bettingOpen = false;
          clearInterval(bettingTimer);
          bettingTimer = null;
          gameMessage.textContent = 'Bets are closed. Waiting for the shared flight to start.';
          syncSlipStates();
          syncSharedRound();
        }
      }, 100);
    }

    async function syncSharedRound() {
      if (syncingSharedState) {
        return;
      }
      syncingSharedState = true;
      const requestStartedAt = performance.now();
      try {
        const formData = new FormData();
        formData.append('action', 'state');
        const response = await fetch(window.aviatorConfig.playUrl, {
          method: 'POST',
          headers: { 'X-CSRFToken': getCsrfToken() },
          body: formData,
        });
        if (!response.ok) {
          throw new Error(`Unable to load shared round state (${response.status}).`);
        }
        const payload = await response.json();
        const responseReceivedAt = performance.now();
        const roundTripMs = responseReceivedAt - requestStartedAt;
        const serverTimeMs = Date.parse(payload.server_time);
        const estimatedDeadline = Date.parse(payload.betting_ends_at);
        const synchronizedDeadline = (
          Number.isFinite(serverTimeMs) && Number.isFinite(estimatedDeadline)
        )
          ? responseReceivedAt + estimatedDeadline - serverTimeMs - (roundTripMs / 2)
          : null;
        const flyingSinceMs = Date.parse(payload.flying_since);
        const elapsedAtServerSnapshot = (
          Number.isFinite(serverTimeMs) && Number.isFinite(flyingSinceMs)
        )
          ? serverTimeMs - flyingSinceMs
          : Number.isFinite(payload.current_multiplier)
            ? ((payload.current_multiplier - 1) / FLIGHT_INCREMENT) * FLIGHT_TICK_MS
            : null;
        const estimatedFlightElapsed = Number.isFinite(elapsedAtServerSnapshot)
          ? Math.max(0, elapsedAtServerSnapshot + (roundTripMs / 2))
          : null;
        if (payload.status === 'betting' && payload.round_id) {
          const newSharedRound = (
            phase !== 'betting'
            || currentSession?.round_id !== payload.round_id
          );
          if (newSharedRound) {
            startBettingWindow(payload, synchronizedDeadline);
          } else {
            bettingDeadlineMonotonic = Number.isFinite(synchronizedDeadline)
              ? synchronizedDeadline
              : responseReceivedAt + (payload.seconds_remaining * 1000);
            bettingOpen = bettingDeadlineMonotonic > responseReceivedAt;
            if (!bettingTimer && bettingOpen) {
              startBettingWindow(payload, synchronizedDeadline);
            }
          }
          currentSession = { round_id: payload.round_id };
          (payload.sessions || []).forEach((session, index) => {
            const slip = slips.find((candidate) => activeSlipSessions.get(candidate) === session.id)
              || slips.find((candidate) => betRequestKeys.get(candidate)?.id === session.bet_request_id)
              || slips[index];
            if (!slip) {
              return;
            }
            placedBetRounds.set(slip, payload.round_id);
            activeSlipSessions.set(slip, session.id);
            if (Number.isFinite(session.stake)) {
              slip.querySelector('.stake-input').value = session.stake.toFixed(2);
            }
            if (Number.isFinite(session.auto_cashout_at)) {
              const autoInput = slip.querySelector('.auto-cashout-input');
              if (autoInput) {
                autoInput.value = session.auto_cashout_at.toFixed(2);
              }
              autoSlips.add(slip);
              if (!autoRepeatStoppedSlips.has(slip)) {
                autoRepeatSlips.add(slip);
              }
            } else if (!autoRepeatSlips.has(slip)) {
              queuedSlips.delete(slip);
            }
          });
          placeQueuedBets(payload.round_id);
          syncSlipStates();
          return;
        }
        if (payload.status === 'flying') {
          if (phase !== 'flying' || currentSession?.round_id !== payload.round_id) {
            activateFlight(payload, [], false, estimatedFlightElapsed);
          } else {
            if (Number.isFinite(estimatedFlightElapsed)) {
              flightStartedAt = responseReceivedAt - estimatedFlightElapsed;
            }
            currentMultiplier = Number.isFinite(estimatedFlightElapsed)
              ? 1 + Math.round(estimatedFlightElapsed / FLIGHT_TICK_MS) * FLIGHT_INCREMENT
              : payload.current_multiplier;
            multiplierValue.textContent = formatMultiplier(currentMultiplier);
            const activeSessionIds = new Set(payload.session_ids || []);
            activeSlipSessions.forEach((sessionId, slip) => {
              if (!activeSessionIds.has(sessionId)) {
                  const settledSession = payload.sessions?.find((session) => session.id === sessionId);
                  if (settledSession?.cashed_out_at != null) {
                    settledBets.set(slip, {
                      cashed_out_at: settledSession.cashed_out_at,
                      won_amount: settledSession.won_amount || 0,
                    });
                  }
                  activeSlipSessions.delete(slip);
                  cashedOutSlips.add(slip);
                }
            });
            syncBetLabels();
          }
        } else if (payload.status === 'crashed') {
          if (phase === 'flying' && currentSession?.round_id === payload.round_id) {
            const reveal = await settleCrashedRound();
            if (reveal?.success) {
              finishCurrentFlight?.(payload.crash_point, Promise.resolve(reveal));
            }
          } else if (
            phase !== 'flying'
            && phase !== 'starting'
            && lastHandledCrashRoundId !== payload.round_id
          ) {
            lastHandledCrashRoundId = payload.round_id;
            phase = 'crashed';
            multiplierValue.textContent = formatMultiplier(payload.crash_point);
            setFairnessText('Seed', payload.server_seed);
            gameMessage.textContent = 'Waiting for the next shared betting window.';
          }
        } else if (phase !== 'flying' && phase !== 'starting') {
          gameMessage.textContent = 'Waiting for the next shared betting window.';
        }
      } catch (error) {
        console.error('Unable to synchronize the shared game round.', error);
      } finally {
        syncingSharedState = false;
      }
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

      let payload;
      try {
        const response = await fetch(window.aviatorConfig.playUrl, {
          method: 'POST',
          headers: {
            'X-CSRFToken': getCsrfToken(),
          },
          body: formData,
        });
        payload = await response.json();
      } catch (error) {
        console.error('Unable to confirm cashout.', error);
        pendingCashoutSlips.delete(slip);
        syncSlipStates();
        gameMessage.textContent = 'Cashout confirmation is delayed. Checking the server for the settled result.';
        syncSharedRound();
        return;
      }
      if (!payload.success) {
        pendingCashoutSlips.delete(slip);
        syncSlipStates();
        if (payload.message?.includes('already been cashed out')) {
          syncSharedRound();
          return;
        }
        gameMessage.textContent = payload.message || 'Unable to cash out.';
        return;
      }

      pendingCashoutSlips.delete(slip);
      gameMessage.textContent = payload.message;
      const balanceDisplay = document.querySelector('#play-balance strong');
      setWalletBalance(payload.balance);
      cashedOutSlips.add(slip);
      settledBets.set(slip, {
        cashed_out_at: payload.cashout_multiplier,
        won_amount: payload.won_amount,
      });
      activeSlipSessions.delete(slip);
      slip.classList.remove('is-running');
      syncSlipStates();
      loadLedger();
    }

    startButtons.forEach((button) => {
      button.addEventListener('click', () => queueBet(button.closest('.aviator-bet-slip')));
    });
    cashoutButtons.forEach((button) => {
      button.addEventListener('click', () => cashOutRound(button));
    });
    ledgerPrevious?.addEventListener('click', () => {
      if (ledgerHasPrevious && ledgerPreviousCursor) {
        ledgerCursor = ledgerPreviousCursor;
        ledgerPage -= 1;
        loadLedger();
      }
    });
    ledgerNext?.addEventListener('click', () => {
      if (ledgerHasNext && ledgerNextCursor) {
        ledgerCursor = ledgerNextCursor;
        ledgerPage += 1;
        loadLedger();
      }
    });
    loadLedger();
    const ledgerInterval = setInterval(loadLedger, 2500);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        loadLedger();
        syncSharedRound();
      }
    });
    window.addEventListener('pagehide', () => clearInterval(ledgerInterval), { once: true });
    let bettingTimer = null;
    let syncingSharedState = false;
    syncSharedRound();
    const sharedStateInterval = setInterval(syncSharedRound, 1000);
    window.addEventListener('pagehide', () => clearInterval(sharedStateInterval), { once: true });
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
            if (payload.room.status === 'running' && Number.isFinite(payload.room.current_multiplier)) {
              currentMultiplier = payload.room.current_multiplier;
              roomMultiplier.textContent = formatMultiplier(currentMultiplier);
            }
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
    if (payload.room.status === 'running' && Number.isFinite(payload.room.current_multiplier)) {
      currentMultiplier = payload.room.current_multiplier;
      roomMultiplier.textContent = formatMultiplier(currentMultiplier);
    }

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
      const formData = new FormData();
      formData.append('action', 'cashout');

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

function attachAccountNavigation() {
  const toggle = document.getElementById('account-nav-toggle');
  const sidebar = document.getElementById('account-sidebar');
  const closeButton = document.getElementById('account-sidebar-close');
  const backdrop = document.getElementById('account-nav-backdrop');

  if (!toggle || !sidebar || !closeButton || !backdrop) {
    return;
  }

  function closeNavigation(restoreFocus = false) {
    sidebar.classList.remove('is-open');
    backdrop.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    document.body.classList.remove('account-nav-open');
    if (restoreFocus) {
      toggle.focus();
    }
  }

  function openNavigation() {
    sidebar.classList.add('is-open');
    backdrop.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    document.body.classList.add('account-nav-open');
    closeButton.focus();
  }

  toggle.addEventListener('click', () => {
    if (sidebar.classList.contains('is-open')) {
      closeNavigation(true);
    } else {
      openNavigation();
    }
  });
  closeButton.addEventListener('click', () => closeNavigation(true));
  backdrop.addEventListener('click', () => closeNavigation(true));
  sidebar.querySelectorAll('a').forEach((link) => {
    link.addEventListener('click', () => closeNavigation());
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && sidebar.classList.contains('is-open')) {
      closeNavigation(true);
    }
  });
}

function attachPaymentForms() {
  document.querySelectorAll('[data-payment-form]').forEach((form) => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]');
      if (!button || button.disabled) {
        return;
      }
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      button.dataset.originalLabel = button.textContent.trim();
      button.textContent = button.dataset.submittingLabel || 'Sending…';
    });
  });
}

function attachPhoneCountryWarnings() {
  document.querySelectorAll('.phone-number-field').forEach((phoneField) => {
    const countrySelect = phoneField.querySelector('.country-code-select');
    const warning = phoneField.querySelector('[data-country-warning]');
    if (!countrySelect || !warning) {
      return;
    }

    const updateWarning = () => {
      const selectedOption = countrySelect.options[countrySelect.selectedIndex];
      if (countrySelect.value === '256' || !selectedOption) {
        warning.hidden = true;
        warning.textContent = '';
        return;
      }

      const countryName = selectedOption.textContent.split(' (+', 1)[0];
      warning.textContent = `We have not started operating in ${countryName} yet. Deposits and withdrawals are currently available in Uganda only.`;
      warning.hidden = false;
    };

    countrySelect.addEventListener('change', updateWarning);
    updateWarning();
  });
}

function attachDismissibleAlerts() {
  document.querySelectorAll('[data-dismissible-alert]').forEach((alert) => {
    const dismissButton = alert.querySelector('.alert-dismiss');
    dismissButton?.addEventListener('click', () => {
      alert.classList.add('is-dismissed');
      alert.addEventListener('transitionend', () => alert.remove(), { once: true });
      window.setTimeout(() => alert.remove(), 220);
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

function attachReferralLinkCopy() {
  document.querySelectorAll('[data-copy-referral]').forEach((button) => {
    button.addEventListener('click', async () => {
      const input = document.getElementById('referral-invite-link');
      const status = document.querySelector('[data-referral-copy-status]');
      if (!input || !status) return;

      try {
        await navigator.clipboard.writeText(input.value);
        status.textContent = 'Invite link copied.';
      } catch {
        input.focus();
        input.select();
        status.textContent = 'Copy is unavailable here. The link is selected so you can copy it manually.';
      }
    });
  });
}

function attachClientSeedSettings() {
  document.querySelectorAll('.fair-seed-form').forEach((form) => {
    const input = form.querySelector('[name="client_seed"]');
    const status = form.querySelector('[data-client-seed-status]');
    const saveButton = form.querySelector('.fair-seed-save');
    const randomizeButton = form.querySelector('[data-randomize-client-seed]');
    if (!input || !status || !saveButton) return;

    randomizeButton?.addEventListener('click', () => {
      const bytes = new Uint8Array(24);
      window.crypto.getRandomValues(bytes);
      input.value = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
      status.textContent = 'A new seed was generated. Save it to use it for future rounds.';
      input.focus();
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;

      saveButton.disabled = true;
      status.textContent = 'Saving client seed…';
      try {
        const response = await fetch(form.dataset.clientSeedUrl, {
          method: 'POST',
          headers: {
            'X-CSRFToken': getCsrfToken(),
          },
          body: new FormData(form),
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) {
          status.textContent = payload.message || 'Unable to save the client seed. Please try again.';
          return;
        }
        input.value = payload.client_seed;
        status.textContent = payload.message;
      } catch {
        status.textContent = 'Could not reach the server. Your seed was not saved; please try again.';
      } finally {
        saveButton.disabled = false;
      }
    });
  });
}

window.addEventListener('DOMContentLoaded', () => {
  attachAccountNavigation();
  attachPaymentForms();
  attachPhoneCountryWarnings();
  attachDismissibleAlerts();
  attachPlayWidget();
  attachRoomWidget();
  attachAviatorMenu();
  attachAviatorModals();
  attachReferralLinkCopy();
  attachClientSeedSettings();
});
