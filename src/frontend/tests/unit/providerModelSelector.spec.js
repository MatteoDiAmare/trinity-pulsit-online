// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ModelSelector from '../../src/components/ModelSelector.vue'

describe('per-task model selection', () => {
  it('offers provider execution on task surfaces and emits the chosen id', async () => {
    const wrapper = mount(ModelSelector, { props: { providerTasks: true } })
    await wrapper.get('input').trigger('focus')
    const grok = wrapper.findAll('button').find(button => button.text().includes('Grok 4.7'))
    expect(grok).toBeDefined()
    await grok.trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([['xai/grok-4.7']])
    wrapper.unmount()
  })

  it('keeps task-only providers out of the native chat picker', async () => {
    const wrapper = mount(ModelSelector)
    await wrapper.get('input').trigger('focus')
    expect(wrapper.text()).not.toContain('Grok 4.7')
    expect(wrapper.text()).toContain('Claude Opus')
    wrapper.unmount()
  })
})
