import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
import { startLiveBoot } from './live/boot'
import { initAuth } from './live/auth'
import { LIVE_MODULES } from './live/modules'

// A sign-in held in sessionStorage (a reload mid-demo) is picked up before the first reads, so
// they carry the token. Render the design immediately; live values swap in as each read lands.
initAuth()
startLiveBoot(LIVE_MODULES)

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
