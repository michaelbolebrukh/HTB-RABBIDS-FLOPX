// Snapshot of OpenRouter's public catalog and per-model endpoints (no API key needed).
// Writes src/data/openrouter.json, which the Intelligence Market page reads.
//   node scripts/openrouter-snapshot.mjs
import fs from 'node:fs'

const API = 'https://openrouter.ai/api/v1'
const OUT = new URL('../src/data/openrouter.json', import.meta.url)
const num = (s) => (s == null ? null : Number(s) * 1e6) // $/token -> $/M tokens

async function get(path, tries = 3) {
  for (let i = 0; ; i++) {
    try {
      const r = await fetch(API + path, { headers: { 'User-Agent': 'flopx-snapshot' } })
      if (!r.ok) throw new Error(`${r.status} ${path}`)
      return (await r.json()).data
    } catch (e) {
      if (i + 1 >= tries) throw e
      await new Promise((res) => setTimeout(res, 800 * (i + 1)))
    }
  }
}

const LAB_ALIAS = { SpaceXAI: 'xAI', 'ByteDance Seed': 'ByteDance', Qwen: 'Qwen (Alibaba)', 'Mistral AI': 'Mistral', MistralAI: 'Mistral', 'Z.AI': 'Z.ai', 'Meta Llama': 'Meta', 'Moonshot': 'Moonshot AI', MoonshotAI: 'Moonshot AI' }
function lab(m) {
  const raw = m.name.includes(':') ? m.name.split(':')[0].trim() : m.id.split('/')[0]
  return LAB_ALIAS[raw] ?? raw
}

const catalogAt = new Date().toISOString()
const all = await get('/models')
const models = all
  .filter((m) => m.architecture?.output_modalities?.join() === 'text')
  // Variant routes (:batch, :free, :thinking…) and ~latest aliases duplicate a listed model.
  .filter((m) => !m.id.startsWith('~') && !m.id.includes(':') && !m.id.startsWith('openrouter/'))
  .map((m) => ({
    id: m.id,
    name: m.name.includes(':') ? m.name.split(':').slice(1).join(':').trim() : m.name,
    lab: lab(m),
    created: m.created,
    open: !!m.hugging_face_id,
    context: m.context_length,
    prompt: num(m.pricing.prompt),
    completion: num(m.pricing.completion),
  }))
  .filter((m) => m.prompt != null && m.completion != null && m.prompt >= 0 && m.completion >= 0 && m.prompt + m.completion > 0)
const aliases = all.filter((m) => m.id.startsWith('~')).length

const open = models.filter((m) => m.open)
const endpoints = []
let i = 0
async function worker() {
  while (i < open.length) {
    const m = open[i++]
    try {
      const d = await get(`/models/${m.id}/endpoints`)
      for (const e of d.endpoints) {
        const p = num(e.pricing.prompt), c = num(e.pricing.completion)
        if (p == null || c == null || p + c <= 0) continue
        const params = e.supported_parameters ?? []
        endpoints.push({
          model: m.id,
          provider: e.provider_name,
          tag: e.tag,
          quant: e.quantization ?? 'unknown',
          tier: e.service_tier ?? 'standard',
          context: e.context_length,
          uptime: e.uptime_last_1d,
          up: e.status === 0,
          tools: params.includes('tools'),
          structured: params.includes('structured_outputs') || params.includes('response_format'),
          caching: !!e.supports_implicit_caching || e.pricing.input_cache_read != null,
          timeOfDay: Array.isArray(e.pricing.overrides) && e.pricing.overrides.length > 0,
          discount: e.pricing.discount ?? 0,
          prompt: p,
          completion: c,
        })
      }
    } catch (err) {
      console.warn('skip', m.id, err.message)
    }
  }
}
await Promise.all(Array.from({ length: 6 }, worker))
const endpointsAt = new Date().toISOString()

fs.writeFileSync(OUT, JSON.stringify({ catalogAt, endpointsAt, aliases, models, endpoints }))
console.log(`${models.length} models (${open.length} open), ${endpoints.length} endpoints -> ${OUT.pathname}`)
