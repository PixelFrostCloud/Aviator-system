(() => {
  const toggle = document.getElementById('admin-menu-toggle');
  const rail = document.querySelector('.admin-rail');
  const backdrop = document.querySelector('.admin-nav-backdrop');

  if (!toggle || !rail || !backdrop) {
    return;
  }

  rail.id = 'admin-navigation';

  const closeMenu = () => {
    rail.classList.remove('is-open');
    backdrop.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', 'Open admin menu');
    document.body.classList.remove('admin-nav-open');
  };

  const openMenu = () => {
    rail.classList.add('is-open');
    backdrop.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    toggle.setAttribute('aria-label', 'Close admin menu');
    document.body.classList.add('admin-nav-open');
  };

  toggle.addEventListener('click', () => {
    if (rail.classList.contains('is-open')) {
      closeMenu();
    } else {
      openMenu();
    }
  });

  backdrop.addEventListener('click', closeMenu);
  rail.querySelectorAll('.admin-nav a').forEach((link) => {
    link.addEventListener('click', closeMenu);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && rail.classList.contains('is-open')) {
      closeMenu();
      toggle.focus();
    }
  });

  window.addEventListener('resize', () => {
    if (window.innerWidth > 900) {
      closeMenu();
    }
  });
})();
