import { beforeEach, describe, expect, it } from 'vitest'

import { useConversationStreamStore } from '@/stores/conversation-stream'

/**
 * Conversation stream store — T24 / #21.
 *
 * The store is the live-progress fold point for SSE events
 * (ADR-0029: "组件订阅 store 里的 plan / tool 状态, 不用各自维护
 * EventSource"). These tests pin the reducer semantics directly —
 * the hook tests cover the transport, the drawer tests cover
 * rendering, and the events landing in between are just these
 * mutations. Plan *content* is intentionally absent here: issue
 * #53 pins `usePlanDrawerStore` as the Plan's single write point.
 */

beforeEach(() => {
  useConversationStreamStore.getState().reset()
})

describe('useConversationStreamStore', () => {
  it('startConversation opens the conversation and resets per-conversation buffers', () => {
    useConversationStreamStore.getState().markNodeRunning('n1')
    useConversationStreamStore.getState().appendAnswerToken('u1', 'x')
    useConversationStreamStore.getState().startConversation('c2')

    const state = useConversationStreamStore.getState()
    expect(state.conversationId).toBe('c2')
    expect(state.nodeStatuses).toEqual({})
    expect(state.streamingAnswer).toBeNull()
    expect(state.connectionStatus).toBe('connecting')
  })

  it('flips node status running → succeeded per the tool.* lifecycle (节点实时切状态)', () => {
    useConversationStreamStore.getState().markNodeRunning('n1')
    expect(useConversationStreamStore.getState().nodeStatuses.n1).toBe('running')

    useConversationStreamStore.getState().markNodeFinished('n1', 'succeeded')
    expect(useConversationStreamStore.getState().nodeStatuses.n1).toBe('succeeded')
  })

  it('marks a node failed from tool.failed', () => {
    useConversationStreamStore.getState().markNodeRunning('n1')
    useConversationStreamStore.getState().markNodeFailed('n1')
    expect(useConversationStreamStore.getState().nodeStatuses.n1).toBe('failed')
  })

  it('beginPlan wipes stale node badges for a new execution', () => {
    useConversationStreamStore.getState().markNodeFinished('n1', 'succeeded')
    useConversationStreamStore.getState().beginPlan()
    expect(useConversationStreamStore.getState().nodeStatuses).toEqual({})
  })

  it('streams llm tokens into one buffer per turn (回答逐字流出)', () => {
    useConversationStreamStore.getState().appendAnswerToken('u1', '你')
    useConversationStreamStore.getState().appendAnswerToken('u1', '好')
    useConversationStreamStore.getState().appendAnswerToken('u1', '!')

    const answer = useConversationStreamStore.getState().streamingAnswer
    expect(answer).toEqual({ turnId: 'u1', text: '你好!', done: false })
  })

  it('a new turn_id replaces the buffer with a fresh one', () => {
    useConversationStreamStore.getState().appendAnswerToken('u1', '第一轮')
    useConversationStreamStore.getState().appendAnswerToken('u2', '第')

    const answer = useConversationStreamStore.getState().streamingAnswer
    expect(answer).toEqual({ turnId: 'u2', text: '第', done: false })
  })

  it('finishActiveAnswer freezes the current buffer', () => {
    useConversationStreamStore.getState().appendAnswerToken('u1', 'done?')
    useConversationStreamStore.getState().finishActiveAnswer()
    expect(useConversationStreamStore.getState().streamingAnswer?.done).toBe(true)
  })

  it('finishActiveAnswer is a no-op when no answer ever streamed', () => {
    useConversationStreamStore.getState().finishActiveAnswer()
    expect(useConversationStreamStore.getState().streamingAnswer).toBeNull()
  })
})
