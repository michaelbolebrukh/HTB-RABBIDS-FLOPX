import { useSyncExternalStore } from 'react';

/**
 * Phone breakpoint, shared by every mobile branch. It is the exact complement of
 * Tailwind's `md:` (min-width: 768px), so `useIsPhone()` is true precisely when
 * `max-md:` variants apply and `md:` variants do not. Desktop (>= 768px) must
 * render its existing branch byte for byte when this is false.
 */
export const PHONE_QUERY = '(max-width: 767.98px)';

function getMql(): MediaQueryList | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null;
  return window.matchMedia(PHONE_QUERY);
}

/** Synchronous read, for non-React code. False when matchMedia is unavailable (SSR, tests). */
export function isPhoneNow(): boolean {
  return getMql()?.matches ?? false;
}

/** Calls `onChange` whenever the viewport crosses the breakpoint; returns the unsubscribe. */
export function subscribePhone(onChange: () => void): () => void {
  const mql = getMql();
  if (!mql) return () => {};
  if (typeof mql.addEventListener === 'function') {
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }
  // Older Safari.
  mql.addListener(onChange);
  return () => mql.removeListener(onChange);
}

/** True on phone widths (< 768px). Re-renders when the viewport crosses the breakpoint. */
export function useIsPhone(): boolean {
  return useSyncExternalStore(subscribePhone, isPhoneNow, () => false);
}
