import type { NewsItem, PredMarket } from '@/data/finance'

export type FeedItem = { kind: 'news'; n: NewsItem } | { kind: 'pm'; p: PredMarket }

/** `key` items lead the feed in their own box; `routine` items sit below it. */
export type FeedTier = 'key' | 'routine'

/** The one place that decides importance: every prediction market, and news with high impact. */
export function tierOf(it: FeedItem): FeedTier {
  if (it.kind === 'pm') return 'key'
  return it.n.impact === 'high' ? 'key' : 'routine'
}

export function splitFeed(items: FeedItem[]): Record<FeedTier, FeedItem[]> {
  return {
    key: items.filter((it) => tierOf(it) === 'key'),
    routine: items.filter((it) => tierOf(it) === 'routine'),
  }
}
