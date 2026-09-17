const menu = document.getElementById('menuButton');
const sidebar = document.getElementById('sidebar');
if (menu && sidebar) menu.addEventListener('click', () => sidebar.classList.toggle('open'));
document.querySelectorAll('form[data-loading]').forEach(form => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"]');
    if (button) { button.disabled = true; button.dataset.oldText = button.textContent; button.textContent = 'Working…'; }
  });
});
