/* A show / hide icon inside every password field.
 *
 * Forms here are rendered from template strings, on every route change and
 * inside dialogs, so there is no single place to add the button. Instead one
 * observer finds each <input type="password"> as it appears, wraps it, and puts
 * the eye inside the field. Fields stay masked by default; the icon flips a
 * field to plain text and back, and it goes back to masked when the field
 * loses focus to somewhere outside the wrapper, so a revealed secret is not
 * left on screen behind a changed page.
 */

const EYE = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" '
  + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/>'
  + '<circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" '
  + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.9 17.9A10.9 10.9 0 0 1 12 19c-6.4 0-10-7-10-7a18.5 18.5 0 0 1 4.1-5.1"/>'
  + '<path d="M9.9 5.2A10 10 0 0 1 12 5c6.4 0 10 7 10 7a18.4 18.4 0 0 1-2.2 3.2"/><path d="M14.1 14.1a3 3 0 1 1-4.2-4.2"/>'
  + '<path d="M2 2l20 20"/></svg>';

function enhance(input) {
  if (input.dataset.pw || input.type !== 'password') return;
  input.dataset.pw = '1';

  const wrap = document.createElement('span');
  wrap.className = 'pw-wrap';
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);

  const button = document.createElement('button');
  button.type = 'button';            // never submits the form
  button.className = 'pw-toggle';
  button.innerHTML = EYE;
  button.setAttribute('aria-label', 'Show Password');
  button.setAttribute('aria-pressed', 'false');
  button.title = 'Show Password';
  wrap.appendChild(button);

  const set = (visible) => {
    input.type = visible ? 'text' : 'password';
    button.innerHTML = visible ? EYE_OFF : EYE;
    button.setAttribute('aria-pressed', String(visible));
    const label = visible ? 'Hide Password' : 'Show Password';
    button.setAttribute('aria-label', label);
    button.title = label;
  };
  // mousedown would steal focus from the field; keep the caret where it was.
  button.addEventListener('mousedown', (e) => e.preventDefault());
  button.addEventListener('click', () => { set(input.type === 'password'); input.focus(); });
  wrap.addEventListener('focusout', (e) => {
    if (!wrap.contains(e.relatedTarget)) set(false);
  });
}

function scan(root) {
  if (root.nodeType !== 1) return;
  if (root.matches?.('input[type="password"]')) enhance(root);
  root.querySelectorAll?.('input[type="password"]').forEach(enhance);
}

scan(document.body);
new MutationObserver((records) => {
  for (const record of records) record.addedNodes.forEach(scan);
}).observe(document.body, { childList: true, subtree: true });
