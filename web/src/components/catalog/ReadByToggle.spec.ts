import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import { ClientKind } from '@generated/trader_joe/proto/ui/v1/data_pb'

import ReadByToggle from './ReadByToggle.vue'

// tj-grna9p.89: the Read By choice in the Dataset detail's Usage header. The same options and rules
// as the Datasets sidebar (tj-grna9p.88): SDK and UI, default SDK, the last ticked kind stays ticked.
function toggle(modelValue: ClientKind[]) {
  return mount(ReadByToggle, { props: { modelValue } })
}
const box = (w: ReturnType<typeof toggle>, kind: 'SDK' | 'UI') => w.find(`[data-read-by="${kind}"] input`)

describe('ReadByToggle', () => {
  it('a labelled group of the two kinds, SDK first, each a labelled checkbox', () => {
    const w = toggle([ClientKind.SDK])
    const group = w.find('[data-testid="read-by"]')
    expect(group.attributes('role')).toBe('group')
    expect(group.attributes('aria-label')).toBe('Read By')
    expect(w.findAll('label').map((l) => l.text())).toEqual(['SDK', 'UI'])
    expect(w.findAll('label input[type="checkbox"]')).toHaveLength(2)
  })

  it('shows the choice; an empty choice reads as the default SDK', () => {
    expect((box(toggle([ClientKind.UI]), 'UI').element as HTMLInputElement).checked).toBe(true)
    expect((box(toggle([ClientKind.UI]), 'SDK').element as HTMLInputElement).checked).toBe(false)
    expect((box(toggle([]), 'SDK').element as HTMLInputElement).checked).toBe(true)
  })

  it('ticking a kind adds it to the choice', async () => {
    const w = toggle([ClientKind.UI])
    await box(w, 'SDK').trigger('change')
    const emitted = w.emitted('update:modelValue') as ClientKind[][][]
    expect(emitted).toHaveLength(1)
    expect([...emitted[0]![0]!].sort()).toEqual([ClientKind.UI, ClientKind.SDK].sort())
  })

  it('unticking one of two leaves the other', async () => {
    const w = toggle([ClientKind.SDK, ClientKind.UI])
    await box(w, 'SDK').trigger('change')
    expect(w.emitted('update:modelValue')).toEqual([[[ClientKind.UI]]])
  })

  it('the last ticked kind is disabled and cannot be cleared', async () => {
    const w = toggle([ClientKind.SDK])
    expect(box(w, 'SDK').attributes('disabled')).toBeDefined()
    expect(box(w, 'UI').attributes('disabled')).toBeUndefined()
    await box(w, 'SDK').trigger('change')
    expect(w.emitted('update:modelValue')).toBeUndefined()
  })
})
