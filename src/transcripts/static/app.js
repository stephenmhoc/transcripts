// Small enhancements; library search and reading remain normal document links.
document.addEventListener('keydown', event => {
  const tag = document.activeElement?.tagName;
  if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) {
    const search = document.getElementById('library-search');
    if (search) { event.preventDefault(); search.focus(); }
  }
});
for (const form of document.querySelectorAll('form[data-action]')) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button[type=submit]');
    const label = button?.textContent;
    if (button) { button.disabled = true; button.textContent = 'Working…'; }
    let error = form.querySelector('[role=alert]');
    if (!error) { error = document.createElement('p'); error.className = 'error'; error.setAttribute('role', 'alert'); form.append(error); }
    error.textContent = '';
    try {
      const response = await fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'X-CSRF-Token': form.querySelector('[name=csrf]')?.value || '' }, credentials: 'same-origin' });
      if (response.redirected) { location.assign(response.url); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(typeof body.detail === 'string' ? body.detail : 'The request could not complete. Please check the pipeline.');
      }
      location.reload();
    } catch (err) {
      error.textContent = err.message || 'Connection interrupted. Try again.';
      if (button) { button.disabled = false; button.textContent = label; }
    }
  });
}
for (const button of document.querySelectorAll('[data-copy]')) {
  button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(button.dataset.copy); button.textContent = 'Copied'; }
    catch { button.textContent = 'Select the link to copy'; }
  });
}
const find = document.getElementById('reader-find');
if (find) {
  const passages = [...document.querySelectorAll('.transcript-segment p')].map(element => ({ element, text: element.textContent }));
  find.addEventListener('input', () => {
    const query = find.value.trim().toLocaleLowerCase();
    let count = 0;
    for (const { element, text } of passages) {
      element.replaceChildren();
      if (!query) { element.textContent = text; continue; }
      let from = 0, at;
      const lowered = text.toLocaleLowerCase();
      while ((at = lowered.indexOf(query, from)) !== -1) {
        element.append(document.createTextNode(text.slice(from, at)));
        const mark = document.createElement('mark'); mark.textContent = text.slice(at, at + query.length); element.append(mark);
        from = at + query.length; count++;
      }
      element.append(document.createTextNode(text.slice(from)));
    }
    document.getElementById('find-count').textContent = query ? `${count} ${count === 1 ? 'match' : 'matches'}` : '';
  });
}
const qrForm = document.getElementById('qr-connect-form');
if (qrForm) {
  qrForm.addEventListener('submit', async event => {
    event.preventDefault();
    const button = qrForm.querySelector('button');
    const csrf = qrForm.querySelector('[name=csrf]').value;
    const error = document.getElementById('qr-error');
    button.disabled = true; error.textContent = '';
    const request = async path => {
      const response = await fetch(path, { method: 'POST', headers: { 'X-CSRF-Token': csrf }, credentials: 'same-origin' });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || 'Could not connect to Overcast.');
      return body;
    };
    try {
      const session = await request('/actions/overcast/qr');
      document.getElementById('qr-image').src = session.image;
      document.getElementById('qr-link').href = session.link;
      document.getElementById('qr-session').hidden = false;
      button.textContent = 'Waiting for approval…';
      const started = Date.now();
      const check = async () => {
        try {
          if (Date.now() - started > 600000) throw new Error('QR code expired. Start again.');
          const result = await request(`/actions/overcast/qr/${encodeURIComponent(session.flow_id)}`);
          if (result.connected) { location.assign('/connections?notice=Overcast%20connected.%20Discovery%20will%20check%20within%20a%20minute.'); return; }
          setTimeout(check, 5000);
        } catch (err) { error.textContent = err.message; button.disabled = false; button.textContent = 'Start a new QR sign-in ↗'; }
      };
      setTimeout(check, 5000);
    } catch (err) { error.textContent = err.message; button.disabled = false; button.textContent = 'Connect with Overcast app ↗'; }
  });
}
