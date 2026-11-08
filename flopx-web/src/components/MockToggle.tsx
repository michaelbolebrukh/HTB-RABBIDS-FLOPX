// The "Show mock book" toggle and the hook the Trading and Fund pages share. The choice lives in
// src/data/mockDesk.ts (one preference for both pages, kept for the session); the default follows
// the account state: on while signed out, while the account is being read, and for an account that
// holds nothing; off once the paper account holds a position or an order (the real book wins).
import { useSyncExternalStore } from 'react'
import { cx, Fresh } from '@/components/ui'
import type { AccountState } from '@/live/account'
import { getMockPref, mockBook, mockDefault, mockDeskLabel, setMockPref, subscribeMockPref, type MockBook } from '@/data/mockDesk'

/** The bundle a page renders through its live-account path: the real account, or the mock book in the same shape. */
export type Book = { email: string; account: MockBook['account']; orders: MockBook['orders']; ordersError: string | null; fresh: MockBook['fresh'] }

export function useMockBook(acct: AccountState): { showMock: boolean; setShowMock: (v: boolean) => void; mock: MockBook | null; book: Book | null; live: Extract<AccountState, { status: 'live' }> | null } {
  const pref = useSyncExternalStore(subscribeMockPref, getMockPref, getMockPref)
  const showMock = pref ?? mockDefault(acct)
  const live = acct.status === 'live' ? acct : null
  const mock = showMock ? mockBook() : null
  const book: Book | null = mock ? { email: 'mock book', account: mock.account, orders: mock.orders, ordersError: null, fresh: mock.fresh } : live
  return { showMock, setShowMock: setMockPref, mock, book, live }
}

/** A switch in the page header. Reads "Show mock book"; the quiet note beside it names what the mock is while it is on. */
export function MockToggle({ on, onChange, className }: { on: boolean; onChange: (v: boolean) => void; className?: string }) {
  return (
    <label className={cx('inline-flex h-8 cursor-pointer items-center gap-2 rounded-[4px] border border-line2 px-2.5 text-[11.5px] text-bone select-none hover:border-mute max-md:h-11', className)} title={on ? mockDeskLabel : 'Show an illustrative book in place of the paper account'}>
      <input type="checkbox" role="switch" aria-checked={on} checked={on} onChange={(e) => onChange(e.target.checked)} className="peer sr-only" />
      <span aria-hidden="true" className={cx('relative inline-block h-3.5 w-6 rounded-full border transition-colors peer-focus-visible:outline peer-focus-visible:outline-1 peer-focus-visible:outline-fct', on ? 'border-dec bg-dec/30' : 'border-line2 bg-ink')}>
        <span className={cx('absolute top-[1px] size-2.5 rounded-full transition-[left]', on ? 'left-[11px] bg-dec' : 'left-[1px] bg-mute')} />
      </span>
      Show mock book
    </label>
  )
}

/** The mock book's quiet note, for a panel that has no eyebrow to carry the book's FreshTag (the KPI strips). */
export function MockNote({ className }: { className?: string }) {
  return <Fresh quiet f="illustrative" label={mockDeskLabel} className={className} />
}
