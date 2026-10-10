// UI copy conventions (tj-mujie8 ruling, tj-grna9p.60).
//
// 1. A screen's page title is EXACTLY its tab name, no variants (DATA_TABS below). A page about
//    one object uses the object's name as its title.
// 2. TITLE CASE for page and panel titles, tab names, buttons, table column headers, filter
//    group labels, saved view names, tile labels and field labels. Small words (and, by, in,
//    of, per, the, to) stay lower case unless first or last; acronyms such as CSV and tokens
//    containing digits are left alone. Run those strings through titleCase().
// 3. SENTENCE case for sentences, hints, messages and filter option text. Not touched here.
//
// Trading group labels in the toggle are Simulation, Paper and Live (semantics.ts GROUP_SPECS).

const SMALL_WORDS: ReadonlySet<string> = new Set(['and', 'by', 'in', 'of', 'per', 'the', 'to'])

// Splits a whitespace-delimited token into leading punctuation, the word and trailing punctuation.
const TOKEN = /^([^\p{L}\p{N}]*)(.*?)([^\p{L}\p{N}]*)$/u

function titleCaseWord(word: string, forceCapital: boolean): string {
  if (word === '') return word
  // Left alone: tokens with a digit (1d, 90+), and acronyms or mixed case (CSV, iOS), i.e. any
  // token that already has an upper-case letter after its first character.
  if (/\d/.test(word) || /^.\p{Lu}/u.test(word) || /^\p{Lu}+$/u.test(word)) return word
  if (!forceCapital && SMALL_WORDS.has(word.toLowerCase())) return word.toLowerCase()
  return word.charAt(0).toUpperCase() + word.slice(1)
}

// Title Case per the convention above. A hyphenated word is cased part by part.
export function titleCase(text: string): string {
  const tokens = text.split(/(\s+)/)
  const wordIndexes = tokens.flatMap((t, i) => (/\S/.test(t) ? [i] : []))
  const first = wordIndexes[0]
  const last = wordIndexes[wordIndexes.length - 1]
  return tokens
    .map((token, i) => {
      if (!/\S/.test(token)) return token
      const [, lead = '', core = '', trail = ''] = TOKEN.exec(token) ?? []
      const force = i === first || i === last
      const cased = core
        .split('-')
        .map((part, partIndex) => titleCaseWord(part, force && partIndex === 0))
        .join('-')
      return lead + cased + trail
    })
    .join('')
}

export function isTitleCase(text: string): boolean {
  return titleCase(text) === text
}

// The Data section's tab names, which are also the exact page titles.
export const DATA_TABS = ['Datasets', 'Requests', 'Health', 'Usage'] as const
export type DataTab = (typeof DATA_TABS)[number]
