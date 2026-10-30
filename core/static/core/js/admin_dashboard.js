(() => {
  const rows = [...document.querySelectorAll('[data-flow-row]')];
  const amounts = rows.map((row) => Number.parseFloat(row.dataset.value) || 0);
  const maximum = Math.max(...amounts, 0);

  rows.forEach((row, index) => {
    const percentage = maximum > 0 ? (amounts[index] / maximum) * 100 : 0;
    row.style.setProperty('--flow-width', `${percentage}%`);
  });

  const date = document.querySelector('#admin-local-date');
  if (date) {
    date.textContent = new Intl.DateTimeFormat(undefined, {
      weekday: 'short',
      day: '2-digit',
      month: 'short',
      year: 'numeric',
    }).format(new Date());
  }
})();