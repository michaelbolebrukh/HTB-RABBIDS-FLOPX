// Every live module, in one list. main.tsx boots them all in parallel.
// To add a domain: write src/live/<domain>.ts exporting a LiveModule, import it here and append
// it to LIVE_MODULES (a one-line targeted edit; never rewrite this file wholesale).
import type { LiveModule } from './boot'
import { gpuModule } from './gpu'
import { regionsModule } from './adapt-w1'
import { financeModule, signalModule } from './adapt-w2'
import { w3Modules } from './adapt-w3'
import { procureModule } from './adapt-w5'
import { accountModule } from './account'
import { strategiesModule } from './strategies'

export const LIVE_MODULES: LiveModule[] = [
  gpuModule,
  regionsModule,
  financeModule,
  ...w3Modules,
  procureModule,
  signalModule,
  accountModule,
  strategiesModule,
]
