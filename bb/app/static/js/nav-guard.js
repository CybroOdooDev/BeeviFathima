/* Unsaved-changes guard for the Settings forms.
 *
 * A settings page registers what "dirty" means and how to save with
 * setGuard(); the router asks before it leaves the page. Leaving by
 * the browser's own means (reload, closing the tab) gets the browser's
 * generic warning, since a page cannot draw its own there.
 */
import { esc } from './ui.js';

let guardState = null;

export function setGuard(guard) {
  guardState = guard;
}

export function clearGuard() {
  guardState = null;
}

/** The registered guard when it has something unsaved, else null. */
export function dirtyGuard() {
  return guardState && guardState.isDirty() ? guardState : null;
}

window.addEventListener('beforeunload', (event) => {
  if (dirtyGuard()) {
    event.preventDefault();
    event.returnValue = '';
  }
});

/** Ask what to do with unsaved changes: resolves 'save', 'discard' or 'cancel'. */
export function confirmLeave() {
  document.querySelector('dialog.confirm-leave')?.remove();
  return new Promise((resolve) => {
    const dialog = document.createElement('dialog');
    dialog.className = 'wizard confirm-leave';
    dialog.setAttribute('aria-labelledby', 'leaveTitle');
    dialog.style.width = 'min(440px, calc(100vw - 24px))';
    dialog.innerHTML = `
      <div class="wiz-head"><strong id="leaveTitle">${esc('Unsaved changes')}</strong>
        <button type="button" class="link wiz-x" data-answer="cancel" aria-label="Close">&times;</button></div>
      <div class="wiz-body">
        <p style="margin:0">You have changes on this page that haven’t been saved.
          Save them before you leave, or discard them?</p>
      </div>
      <div class="wiz-foot">
        <button type="button" class="link" data-answer="cancel">Cancel</button>
        <div class="actions">
          <button type="button" class="danger" data-answer="discard">Discard</button>
          <button type="button" class="primary" data-answer="save">Save</button>
        </div>
      </div>`;
    document.body.append(dialog);
    let answer = 'cancel';
    const finish = () => { dialog.close(); dialog.remove(); resolve(answer); };
    dialog.querySelectorAll('[data-answer]').forEach((b) => b.addEventListener('click', () => {
      answer = b.dataset.answer;
      finish();
    }));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); finish(); });
    dialog.addEventListener('click', (event) => { if (event.target === dialog) finish(); });
    dialog.showModal();
    dialog.querySelector('[data-answer=save]').focus();
  });
}
