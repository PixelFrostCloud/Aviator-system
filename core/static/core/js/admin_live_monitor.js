(() => {
  const monitor = document.querySelector('[data-live-monitor]');
  if (!monitor) return;

  const endpoint = monitor.dataset.liveUrl;
  const connection = document.querySelector('#admin-monitor-connection');
  const roundId = document.querySelector('#admin-live-round-id');
  const roundShort = document.querySelector('#admin-live-round-short');
  const phaseBadge = document.querySelector('#admin-live-phase');
  const statusValue = document.querySelector('#admin-live-status');
  const multiplierValue = document.querySelector('#admin-live-multiplier');
  const updatedValue = document.querySelector('#admin-live-updated');
  const betsValue = document.querySelector('#admin-live-bets');
  const openBetsValue = document.querySelector('#admin-live-open-bets');
  const cashedOutValue = document.querySelector('#admin-live-cashed-out');
  const stakesValue = document.querySelector('#admin-live-stakes');
  const seedHash = document.querySelector('#admin-live-seed-hash');
  const plane = document.querySelector('#admin-flight-plane');
  const trail = document.querySelector('#admin-flight-trail');
  const trajectory = document.querySelector('.admin-flight-path');
  const emergencyForm = document.querySelector('[data-emergency-void-form]');
  const emergencyRoundId = emergencyForm?.querySelector('[data-emergency-round-id]');
  const emergencyConfirmation = emergencyForm?.querySelector('[data-emergency-confirmation]');
  const emergencySubmit = emergencyForm?.querySelector('[data-emergency-submit]');
  let emergencyRoundIdValue = '';
  let loading = false;

  const phaseLabels = {
    betting: 'Betting open',
    flying: 'In flight',
    crashed: 'Round complete',
    void: 'Voided',
    waiting: 'Waiting for round',
  };

  function setConnectionState(isConnected, label) {
    connection.classList.toggle('is-disconnected', !isConnected);
    connection.querySelector('span').textContent = label;
    connection.setAttribute('aria-live', 'polite');
  }

  function drawFlight(multiplier) {
    const progress = Math.max(0, Math.min(1, Math.log(Math.max(1, multiplier)) / Math.log(286)));
    const pathLength = trajectory.getTotalLength();
    const position = trajectory.getPointAtLength(pathLength * progress);
    plane.setAttribute('transform', `translate(${position.x} ${position.y})`);

    const samples = Math.max(2, Math.ceil(progress * 70));
    const points = [];
    for (let index = 0; index <= samples; index += 1) {
      const point = trajectory.getPointAtLength((pathLength * progress * index) / samples);
      points.push(`${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)} ${point.y.toFixed(1)}`);
    }
    trail.setAttribute('d', points.join(' '));
  }

  function updateMonitor(payload) {
    const status = payload.status in phaseLabels ? payload.status : 'waiting';
    const multiplier = Number.isFinite(payload.current_multiplier) ? payload.current_multiplier : 1;
    const visibleId = payload.round_id || 'No active shared round';

    roundId.textContent = visibleId;
    roundShort.textContent = payload.round_id || '—';
    phaseBadge.textContent = phaseLabels[status];
    phaseBadge.dataset.phase = status;
    statusValue.textContent = phaseLabels[status];
    multiplierValue.textContent = `${multiplier.toFixed(2)}x`;
    updatedValue.textContent = new Intl.DateTimeFormat(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }).format(payload.server_time ? new Date(payload.server_time) : new Date());
    betsValue.textContent = Number(payload.bet_count || 0).toLocaleString();
    openBetsValue.textContent = Number(payload.open_bets || 0).toLocaleString();
    cashedOutValue.textContent = Number(payload.cashed_out || 0).toLocaleString();
    stakesValue.textContent = Number(payload.total_stakes || 0).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    seedHash.textContent = payload.server_seed_hash || 'Available when a round starts';
    drawFlight(multiplier);
    if (emergencyRoundId && emergencyConfirmation && emergencySubmit) {
      const currentRoundId = payload.round_id || '';
      if (currentRoundId !== emergencyRoundIdValue) {
        emergencyRoundIdValue = currentRoundId;
        emergencyRoundId.value = currentRoundId;
        emergencyConfirmation.value = '';
        emergencyConfirmation.placeholder = currentRoundId
          ? `Type VOID ${currentRoundId}`
          : 'Waiting for an active round';
      }
      emergencySubmit.disabled = status !== 'flying' || !currentRoundId;
    }
  }

  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      const response = await fetch(endpoint, {
        method: 'GET',
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!response.ok) throw new Error(`Live feed returned HTTP ${response.status}`);
      const payload = await response.json();
      if (!payload.success) throw new Error(payload.message || 'Live feed response was unsuccessful.');
      updateMonitor(payload);
      setConnectionState(true, 'Live · updating every second');
    } catch (error) {
      setConnectionState(false, 'Live feed unavailable · retrying');
      console.error('Unable to refresh admin live game monitor.', error);
    } finally {
      loading = false;
    }
  }

  refresh();
  window.setInterval(refresh, 1000);
})();
