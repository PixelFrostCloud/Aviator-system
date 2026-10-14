(() => {
  const page = document.querySelector('[data-history-url]');
  if (!page) return;

  const rowsContainer = page.querySelector('[data-history-rows]');
  const syncStatus = page.querySelector('[data-history-sync-status] span');
  const pagination = page.querySelector('[data-history-pagination]');
  const csrfToken = document.querySelector('.bet-history-csrf input[name="csrfmiddlewaretoken"]')?.value || '';
  const query = new URLSearchParams(window.location.search);
  const outcome = page.dataset.outcome || 'all';
  const pageNumber = query.get('page') || '1';
  let requestPending = false;
  let timer = null;

  if (!rowsContainer || !syncStatus) return;

  const formatMoney = (amount) => Number(amount).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  const setCell = (tagName, label, value, className = '') => {
    const cell = document.createElement(tagName);
    cell.dataset.label = label;
    if (className) cell.className = className;
    cell.textContent = value;
    return cell;
  };

  function createBetRow(session, currency) {
    const row = document.createElement('article');
    row.className = 'account-table-row bet-history-row';
    row.dataset.sessionId = session.id;

    row.append(
      setCell('span', 'Time', session.created_at, 'bet-time'),
      setCell('strong', 'Stake', `${formatMoney(session.stake)} ${currency}`),
      setCell('span', 'Round', session.round_id || 'Unavailable', 'bet-round'),
      setCell('span', 'Multiplier', session.multiplier === null ? '--' : `${formatMoney(session.multiplier)}x`),
      setCell(
        'em',
        'Return',
        session.payout === null ? '--' : `${formatMoney(session.payout)} ${currency}`,
        session.status === 'won' ? 'positive' : '',
      ),
    );

    const statusCell = document.createElement('span');
    statusCell.dataset.label = 'Status';
    const status = document.createElement('span');
    status.className = `bet-status ${session.status}`;
    const labels = {
      won: 'Won',
      lost: 'Lost',
      live: 'Live',
      void: 'Voided',
      unavailable: 'Unavailable',
    };
    status.textContent = labels[session.status] || 'Unavailable';
    statusCell.append(status);
    row.append(statusCell);
    return row;
  }

  function updatePagination(data) {
    if (!pagination) return;
    pagination.hidden = data.pages <= 1;
    const label = pagination.querySelector('[data-history-page-label]');
    if (label) label.textContent = `Page ${data.page} of ${data.pages}`;

    const controls = pagination.querySelector('div');
    if (!controls) return;
    controls.replaceChildren();
    for (const [direction, available, nextPage] of [
      ['previous', data.has_previous, data.page - 1],
      ['next', data.has_next, data.page + 1],
    ]) {
      if (!available) continue;
      const link = document.createElement('a');
      link.href = `?outcome=${encodeURIComponent(outcome)}&page=${nextPage}`;
      link.dataset.historyPage = direction;
      link.setAttribute('aria-label', direction === 'previous' ? 'Previous page' : 'Next page');
      link.textContent = direction === 'previous' ? 'Previous' : 'Next';
      controls.append(link);
    }
  }

  function renderHistory(data) {
    const currency = data.currency;
    const statValues = {
      total_bets: String(data.total_bets),
      win_rate: Number(data.win_rate).toFixed(1),
      total_staked: formatMoney(data.total_staked),
      total_returned: formatMoney(data.total_returned),
      net_result: `${formatMoney(data.net_result)} ${currency}`,
    };

    for (const [key, value] of Object.entries(statValues)) {
      const target = page.querySelector(`[data-history-stat="${key}"]`);
      if (target) target.textContent = value;
    }

    const netResult = page.querySelector('[data-history-stat="net_result"]');
    if (netResult) {
      netResult.classList.toggle('positive', Number(data.net_result) >= 0);
      netResult.classList.toggle('negative', Number(data.net_result) < 0);
    }

    const balance = document.querySelector('[data-account-balance]');
    if (balance) balance.textContent = formatMoney(data.balance);

    for (const link of page.querySelectorAll('[data-history-filter]')) {
      const count = data.filter_counts[link.dataset.historyFilter];
      const counter = link.querySelector('span');
      if (counter && Number.isFinite(count)) counter.textContent = String(count);
    }

    const fragment = document.createDocumentFragment();
    if (data.sessions.length) {
      for (const session of data.sessions) fragment.append(createBetRow(session, currency));
    } else {
      const empty = document.createElement('div');
      empty.className = 'account-empty-state';
      empty.dataset.historyEmpty = '';
      empty.textContent = outcome === 'all'
        ? 'No bets yet. Your game activity will appear here when you play.'
        : `No ${outcome} bets right now.`;
      fragment.append(empty);
    }
    rowsContainer.replaceChildren(fragment);
    updatePagination(data);
  }

  async function fetchHistory() {
    const url = new URL(page.dataset.historyUrl, window.location.href);
    url.searchParams.set('outcome', outcome);
    url.searchParams.set('page', pageNumber);

    const response = await fetch(url, {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) throw new Error(`Bet history request failed (${response.status}).`);
    const data = await response.json();
    if (!data.success) throw new Error('The server could not refresh bet history.');
    return data;
  }

  async function settleRound(roundId) {
    const body = new FormData();
    body.append('action', 'settle_crash');
    body.append('round_id', roundId);
    const response = await fetch(page.dataset.playUrl, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRFToken': csrfToken },
      body,
    });
    if (response.status === 409) return false;
    if (!response.ok) throw new Error(`Round settlement request failed (${response.status}).`);
    const data = await response.json();
    if (!data.success) throw new Error(data.message || 'Could not confirm the round result.');
    return true;
  }

  async function refresh() {
    if (requestPending || document.visibilityState === 'hidden') return;
    requestPending = true;
    try {
      let data = await fetchHistory();
      if (data.settle_rounds.length) {
        await Promise.all(data.settle_rounds.map(settleRound));
        data = await fetchHistory();
      }
      renderHistory(data);
      syncStatus.textContent = `Live · Updated ${new Date().toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })}`;
      page.classList.remove('history-sync-error');
    } catch (error) {
      console.error('Unable to refresh game bet history.', error);
      syncStatus.textContent = 'Update unavailable · retrying';
      page.classList.add('history-sync-error');
    } finally {
      requestPending = false;
      if (document.visibilityState !== 'hidden') timer = window.setTimeout(refresh, 5000);
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      window.clearTimeout(timer);
      refresh();
    } else {
      window.clearTimeout(timer);
    }
  });
  window.addEventListener('pagehide', () => window.clearTimeout(timer), { once: true });
  refresh();
})();
