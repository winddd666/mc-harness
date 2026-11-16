'use strict'

// Each HTTP action owns a lease. A disconnected action must never release a
// later action's lock or resume against a replacement Mineflayer connection.
function cancelledError (cause) {
  const error = new Error(`action cancelled: ${cause}`)
  error.code = cause === 'action_timeout' ? 'action_timeout' : 'interrupted'
  error.extra = { cause, requires_replan: true }
  return error
}

function beginAction (session, name) {
  let reject
  const aborted = new Promise((resolve, rejectPromise) => { reject = rejectPromise })
  aborted.catch(() => {})
  const lease = {
    name, t: Date.now(), bot: session.bot, aborted, cancelled: false, cause: null,
    cancel (cause) {
      if (lease.cancelled) return
      lease.cancelled = true
      lease.cause = cause
      reject(cancelledError(cause))
    }
  }
  session.busy = lease
  return lease
}

function assertCurrent (session, lease) {
  if (!lease.cancelled && session.bot !== lease.bot) lease.cancel('disconnected')
  if (lease.cancelled) throw cancelledError(lease.cause)
}

async function runAction (session, lease, operation, timeoutMs = 30000) {
  const scopedSession = new Proxy(session, {
    get (target, key, receiver) {
      assertCurrent(session, lease)
      return Reflect.get(target, key, receiver)
    },
    set (target, key, value, receiver) {
      assertCurrent(session, lease)
      return Reflect.set(target, key, value, receiver)
    }
  })
  const timer = setTimeout(() => {
    // The underlying library promise may be uncooperative. Block further
    // actions until the operator closes this connection; never free it for
    // another action on the same socket while old work could still resume.
    if (session.busy === lease) session.actionQuarantined = true
    lease.cancel('action_timeout')
  }, timeoutMs)
  try {
    return await Promise.race([
      Promise.resolve().then(() => operation(scopedSession)),
      lease.aborted
    ])
  } finally {
    clearTimeout(timer)
  }
}

function finishAction (session, lease) {
  if (session.busy !== lease) return
  session.busy = null
  session.activeStopOn = null
  if (session.bot === lease.bot && !lease.cancelled) session.applyPosture()
}

function releaseDisconnectedAction (session) {
  const lease = session.busy
  if (lease && typeof lease.cancel === 'function') lease.cancel('disconnected')
  session.busy = null
  session.activeStopOn = null
  session.actionQuarantined = false
}

module.exports = { beginAction, runAction, finishAction, releaseDisconnectedAction }
