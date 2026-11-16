'use strict'
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')
const lifecycle = require('./action-lifecycle')

function harness (timeoutMs = 1000) {
  const session = {
    createdAt: Date.now(), alive: true, bot: { health: 20 }, busy: null,
    isReady: () => true, applyPosture () {}, log () {}, nearestHostile: () => null
  }
  class ApiError extends Error { constructor (code, message) { super(message); this.code = code } }
  const api = { hang: async () => new Promise(() => {}), ping: async () => ({ pong: true }) }
  const core = { Session: class {}, sessions: new Map([['test', session]]), api,
    READONLY: new Set(), ApiError, fmt: value => value, log () {} }
  const module = { exports: {} }
  const source = fs.readFileSync(path.resolve(__dirname, '../../experiments/20261115-until-1900/backend_patched/src/http.js'), 'utf8')
  vm.runInNewContext(source + '\nmodule.exports.handleRequest = handleRequest', {
    module, require: name => name === './session' ? core : name === './action-lifecycle' ? lifecycle : require(name),
    URLSearchParams, Buffer, setInterval, console
  })
  module.exports.configure({ config: { session: { tokenTtlHours: 24, actionTimeoutMs: timeoutMs }, http: {} }, authProvider: {} })
  async function request (name) {
    let result
    const req = { url: '/api/v1/' + name, headers: { authorization: 'Bearer test' },
      async * [Symbol.asyncIterator] () { yield Buffer.from('{}') } }
    const res = { writeHead () {}, end (text) { result = JSON.parse(text) } }
    await module.exports.handleRequest(req, res)
    return result
  }
  return { session, api, request }
}

test('HTTP pending action responds after disconnect; next connection can act', async () => {
  const { session, request } = harness()
  const pending = request('hang')
  for (let i = 0; !session.busy && i < 10; i++) await new Promise(resolve => setImmediate(resolve))
  assert.equal(session.busy.name, 'hang')
  session.bot = null
  lifecycle.releaseDisconnectedAction(session)
  const response = await pending
  assert.equal(response.ok, false)
  assert.equal(response.error.cause, 'disconnected')
  assert.equal(response.error.requires_replan, true)
  assert.equal(session.busy, null)
  session.bot = { health: 20 }
  assert.equal((await request('ping')).result.pong, true)
})

test('HTTP timeout refuses further actions until disconnected', async () => {
  const { session, request } = harness(15)
  const timedOut = await request('hang')
  assert.equal(timedOut.ok, false)
  assert.equal(timedOut.error.code, 'action_timeout')
  assert.equal(session.actionQuarantined, true)
  assert.equal((await request('ping')).error.code, 'action_quarantined')
  session.bot = null
  lifecycle.releaseDisconnectedAction(session)
  session.bot = { health: 20 }
  assert.equal((await request('ping')).ok, true)
})
