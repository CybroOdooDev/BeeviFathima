/* Small pieces of guided-setup state that more than one page needs.
 *
 * "Pairing reviewed" is remembered in this browser, per account: every account
 * already has working pairing defaults, so there is nothing on the server that
 * says whether someone has looked at them. Accounts that have already run a
 * sync count as reviewed regardless (see getstarted.js), so nobody who set up
 * before this step existed is asked to do it again.
 */
import { auth } from './api.js';

const key = () => `bb:pairing-reviewed:${auth.tenant?.id}`;

export function pairingReviewed() {
  try { return localStorage.getItem(key()) === '1'; } catch { return false; }
}

export function markPairingReviewed() {
  try { localStorage.setItem(key(), '1'); } catch { /* a convenience only */ }
}
