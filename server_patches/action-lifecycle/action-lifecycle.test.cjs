'use strict'
const assert = require('node:assert/strict')
const { test } = require('node:test')
const { beginAction, runAction, finishAction, releaseDisconnectedAction } = require('./action-lifecycle')

const makeSession = () => ({ bot: { name: 'old' }, busy: null, applied: 0, applyPosture () { this.applied++ } })

test('disconnect rejects a permanently pending action and frees its lock', async () => {
  const session = makeSession()
  const lease = beginAction(session, 'place')
  const pending = runAction(session, lease, () => new Promise(() => {}))
  session.bot = null
  releaseDisconnectedAction(session)
  await assert.rejects(pending, e => e.code === 'interrupted' && e.extra.cause === 'disconnected')
  finishAction(session, lease)
  assert.equal(session.busy, null)
})

test('an old action cannot release the replacement action lock', async () => {
  const session = makeSession()
  const old = beginAction(session, 'place')
  const pending = runAction(session, old, () => new Promise(() => {}))
  releaseDisconnectedAction(session)
  session.bot = { name: 'new' }
  const current = beginAction(session, 'dig')
  await assert.rejects(pending)
  finishAction(session, old)
  assert.equal(session.busy, current)
})

test('late continuation cannot touch the new connection', async () => {
  const session = makeSession()
  let resume
  const gate = new Promise(resolve => { resume = resolve })
  let touched = false
  const lease = beginAction(session, 'place')
  const pending = runAction(session, lease, async scoped => { await gate; scoped.bot.touched = true; touched = true })
  await Promise.resolve()
  releaseDisconnectedAction(session)
  session.bot = { name: 'new' }
  await assert.rejects(pending)
  resume()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(touched, false)
  assert.equal(session.bot.touched, undefined)
})

test('an uncooperative timeout quarantines this connection until disconnect', async () => {
  const session = makeSession()
  const lease = beginAction(session, 'place')
  await assert.rejects(runAction(session, lease, () => new Promise(() => {}), 15), e => e.code === 'action_timeout')
  finishAction(session, lease)
  assert.equal(session.actionQuarantined, true)
  releaseDisconnectedAction(session)
  assert.equal(session.actionQuarantined, false)
})

test('normal action and ordinary errors release the owned lease', async () => {
  const session = makeSession()
  const first = beginAction(session, 'dig')
  assert.deepEqual(await runAction(session, first, async () => ({ gained: 1 })), { gained: 1 })
  finishAction(session, first)
  assert.equal(session.applied, 1)
  const second = beginAction(session, 'craft')
  await assert.rejects(runAction(session, second, async () => { throw new Error('no ingredients') }))
  finishAction(session, second)
  assert.equal(session.busy, null)
})
