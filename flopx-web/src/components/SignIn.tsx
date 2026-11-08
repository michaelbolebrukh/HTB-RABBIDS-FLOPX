import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { LogIn, X } from 'lucide-react'
import { useAuth } from '@/live/auth'
import { Btn, cx } from './ui'

// The top-bar sign-in control: a small button in the shell's own vocabulary (the height, border
// and type of the search control beside it), a compact chip once signed in, and a minimal dialog
// in the design's overlay style (Overlays.tsx: the same scrim, panel, header and inputs as the
// paper order ticket). The password lives in this component's state for the length of the dialog
// and goes into one request; it is never prefilled, stored or logged.

/** What a key does inside the dialog: Enter submits, Escape closes, anything else is typing. */
export function signInKeyAction(key: string): 'submit' | 'close' | null {
  if (key === 'Enter') return 'submit'
  if (key === 'Escape') return 'close'
  return null
}

/** The chip's text: the full address where the bar has room, the local part where it does not. */
export function chipLabel(email: string, compact: boolean): string {
  return compact ? email.split('@')[0] : email
}

const control = 'flex h-8 cursor-pointer items-center gap-1.5 rounded-[5px] border border-line bg-panel px-2.5 text-[12px] text-dim hover:border-line2 hover:text-bone'

export function AuthControl() {
  const auth = useAuth()
  const [open, setOpen] = useState(false)
  if (!auth.signedIn || !auth.email) {
    return (
      <>
        <button onClick={() => setOpen(true)} className={cx(control, 'whitespace-nowrap')} title="Sign in to see the paper account and the archive series">
          <LogIn size={13} /> Sign in
        </button>
        {open && <SignInDialog onClose={() => setOpen(false)} />}
      </>
    )
  }
  return (
    <span className="flex h-8 items-center gap-2 rounded-[5px] border border-line bg-panel px-2.5 font-mono text-[11px] whitespace-nowrap text-mute" title={`Signed in as ${auth.email}`}>
      <span aria-hidden className="size-1.5 rounded-full bg-up" />
      <span className="hidden xl:inline">{chipLabel(auth.email, false)}</span>
      <span className="xl:hidden">{chipLabel(auth.email, true)}</span>
      <span className="text-dim">·</span>
      <button onClick={() => void auth.signOut()} className="cursor-pointer text-bone hover:underline">Sign out</button>
    </span>
  )
}

const input = 'mt-1 h-9 w-full rounded-[4px] border border-line2 bg-ink px-2 text-[13px] text-bone outline-none placeholder:text-dim focus:border-fct/50'

export function SignInDialog({ onClose }: { onClose: () => void }) {
  const { signIn, busy, error } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const emailRef = useRef<HTMLInputElement>(null)
  useEffect(() => { setTimeout(() => emailRef.current?.focus(), 10) }, [])
  const submit = async () => {
    if (busy) return
    const ok = await signIn(email, password)
    if (ok) onClose()
  }
  const onKey = (e: KeyboardEvent) => {
    const action = signInKeyAction(e.key)
    if (!action) return
    e.preventDefault()
    if (action === 'close') onClose()
    else void submit()
  }
  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/60 backdrop-blur-[2px]" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-labelledby="signin-title" className="fade-in w-[400px] max-w-[calc(100vw-24px)] rounded-lg border border-line2 bg-panel shadow-2xl" onClick={(e) => e.stopPropagation()} onKeyDown={onKey}>
        <header className="flex items-center justify-between border-b border-line px-5 py-3">
          <div>
            <div id="signin-title" className="text-[14px] font-semibold">Sign in</div>
            <div className="mt-0.5 font-mono text-[10px] text-dim">FLOPX account · the paper desk and archive series open to a signed-in reader</div>
          </div>
          <button onClick={onClose} className="cursor-pointer text-dim hover:text-bone" aria-label="Close sign-in"><X size={15} /></button>
        </header>
        <form onSubmit={(e) => { e.preventDefault(); void submit() }} className="space-y-4 px-5 py-4">
          <label className="block"><span className="eyebrow">Email</span>
            <input ref={emailRef} type="email" name="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@domain" className={input} />
          </label>
          <label className="block"><span className="eyebrow">Password</span>
            <input type="password" name="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} className={input} />
          </label>
          <div role="alert" aria-live="polite" className={cx('min-h-[16px] font-mono text-[11px]', error ? 'text-down' : 'text-dim')}>{error ?? ''}</div>
          <div className="flex items-center justify-between border-t border-line pt-3">
            <span className="font-mono text-[10px] text-dim">Enter to sign in · Esc to close</span>
            <Btn type="submit" variant="primary" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</Btn>
          </div>
        </form>
      </div>
    </div>
  )
}
