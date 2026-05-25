/**
 * Formatting helpers. Tabular-numeric output everywhere the user reads
 * percentages, deltas, P&L, durations. No localization (single-user bot;
 * en-US is fine).
 */

/** $1,234.56 — signed if `signed=true`. */
export function usd(n: number | null | undefined, signed = false): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const opts: Intl.NumberFormatOptions = {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: Math.abs(n) >= 100 ? 0 : 2,
    minimumFractionDigits: 0,
    signDisplay: signed ? 'always' : 'auto'
  };
  return new Intl.NumberFormat('en-US', opts).format(n);
}

/** 4.21% — always signed for deltas, capped to N decimals. */
export function pct(
  n: number | null | undefined,
  digits = 2,
  signed = true
): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const sign = signed && n > 0 ? '+' : '';
  return `${sign}${n.toFixed(digits)}%`;
}

/** 1.2K / 3.4M / 1.5B — short. */
export function compact(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return new Intl.NumberFormat('en-US', {
    notation: 'compact',
    maximumFractionDigits: 2
  }).format(n);
}

/** "2h ago" / "5m" / "3d" — compact relative-time. */
export function timeAgo(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return '—';
  const then = new Date(iso);
  const secs = Math.max(0, Math.floor((now.getTime() - then.getTime()) / 1000));
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

/** Tone CSS class for a positive/negative number. */
export function tone(n: number | null | undefined): 'pos' | 'neg' | 'mut' {
  if (n === null || n === undefined || Number.isNaN(n)) return 'mut';
  if (n > 0) return 'pos';
  if (n < 0) return 'neg';
  return 'mut';
}

/** Pretty number with the right number of decimals based on magnitude. */
export function price(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  if (n < 1) return n.toPrecision(4);
  if (n < 10) return n.toFixed(4);
  if (n < 100) return n.toFixed(2);
  return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

/** Strip the most common Markdown syntax for plain-text previews
 * inside line-clamped cards. We render full Markdown in drawers
 * where there's room for nested HTML; cards need the visual clamp
 * to work, which means a single text node — hence stripping.
 *
 * Strips: **bold**, *italic*, _italic_, `code`, # headings,
 * - bullets, > blockquotes, [text](url) → text, escaped chars,
 * multiple newlines → one space. */
export function stripMd(s: string | null | undefined): string {
  if (!s) return '';
  return s
    .replace(/```[\s\S]*?```/g, ' ')              // fenced code
    .replace(/`([^`]+)`/g, '$1')                   // inline code
    .replace(/\*\*([^*]+)\*\*/g, '$1')             // **bold**
    .replace(/__([^_]+)__/g, '$1')                 // __bold__
    .replace(/(^|[^*])\*([^*]+)\*([^*]|$)/g, '$1$2$3')  // *italic*
    .replace(/(^|\s)_([^_]+)_(\s|$)/g, '$1$2$3')   // _italic_
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')       // [text](url) → text
    .replace(/^>\s*/gm, '')                        // > quote
    .replace(/^#+\s*/gm, '')                       // # heading
    .replace(/^\s*[-*+]\s+/gm, '· ')               // - bullet → ·
    .replace(/^\s*\d+\.\s+/gm, '· ')               // 1. ordered
    .replace(/\\([_*`#])/g, '$1')                  // escaped chars
    .replace(/\n{2,}/g, ' · ')                     // paragraph → ·
    .replace(/\s+/g, ' ')
    .trim();
}
