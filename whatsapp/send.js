#!/usr/bin/env node
'use strict'

/*
 * whatsapp/send.js — a tiny Baileys bridge for wc_exact_score.py.
 *
 * Baileys (the WhatsApp Web multi-device library) is JavaScript-only, so the
 * Python script shells out to this CLI to post World Cup updates to a group.
 *
 * Commands:
 *   node send.js login     Pair this machine: print a QR to scan from
 *                          WhatsApp > Linked devices, then exit once connected.
 *   node send.js groups    Print the joined groups (name + JID) as JSON.
 *   node send.js send      Read {"jid": "...@g.us", "messages": [...]} from
 *                          stdin and post each message to that group.
 *
 * Auth (the linked-device credentials) is persisted under ./auth by default,
 * overridable with WHATSAPP_AUTH_DIR. That directory is gitignored — treat it
 * like a password: anyone with it can post as your WhatsApp account.
 */

const path = require('path')

const baileys = require('@whiskeysockets/baileys')
const makeWASocket = baileys.default
const { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = baileys

const AUTH_DIR = process.env.WHATSAPP_AUTH_DIR || path.join(__dirname, 'auth')

const delay = (ms) => new Promise((r) => setTimeout(r, ms))

// Baileys expects a pino-like logger. We don't want the dependency or the noise,
// so feed it a silent stub (every method is a no-op, .child() returns itself).
const silentLogger = {
  level: 'silent',
  child() { return silentLogger },
  trace() {}, debug() {}, info() {}, warn() {}, error() {}, fatal() {},
}

function readStdin() {
  return new Promise((resolve) => {
    if (process.stdin.isTTY) { resolve(''); return }
    let data = ''
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', (chunk) => { data += chunk })
    process.stdin.on('end', () => resolve(data))
  })
}

/*
 * Open a connected socket. Resolves with the live socket once 'open' fires.
 *  - allowQR=false (send/groups): an unregistered session needs pairing, so a QR
 *    means "not paired" — reject telling the user to run `login`.
 *  - allowQR=true (login): render the QR via onQR and wait for the user to scan.
 * A restartRequired close (Baileys asks for one reconnect right after pairing)
 * is handled transparently by reconnecting. A hard timeout guards against the
 * connection silently stalling — without it a never-settling promise would let
 * Node exit 0 once the event loop drained, i.e. a no-op that looks like success.
 */
function openSocket({ allowQR = false, onQR = null, timeoutMs = 60000 } = {}) {
  return new Promise((resolve, reject) => {
    let settled = false
    let timer = null
    let liveSock = null
    const armTimer = () => {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        try { if (liveSock) liveSock.end() } catch (_) {}
        finish(reject, new Error(
          `Timed out after ${Math.round(timeoutMs / 1000)}s waiting for WhatsApp.` +
          (allowQR ? ' QR not scanned in time.' : ' Check connectivity, or re-pair with `login`.')))
      }, timeoutMs)
    }
    const finish = (fn, arg) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      fn(arg)
    }

    armTimer()
    ;(async () => {
      const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
      const { version } = await fetchLatestBaileysVersion()
      const sock = makeWASocket({
        version,
        auth: state,
        logger: silentLogger,
        browser: ['wc-exact-score', 'Chrome', '1.0.0'],
      })
      liveSock = sock
      sock.ev.on('creds.update', saveCreds)
      sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update
        if (qr) {
          if (allowQR && onQR) { onQR(qr); armTimer() }  // give the user fresh time to scan
          else {
            try { sock.end() } catch (_) {}
            finish(reject, new Error(
              'Not paired with WhatsApp. Run `node send.js login` and scan the QR first.'))
          }
        }
        if (connection === 'open') {
          finish(resolve, { sock, saveCreds })
        } else if (connection === 'close') {
          const code = lastDisconnect && lastDisconnect.error
            && lastDisconnect.error.output && lastDisconnect.error.output.statusCode
          if (code === DisconnectReason.restartRequired) {
            try { sock.end() } catch (_) {}
            openSocket({ allowQR, onQR, timeoutMs }).then(
              (s) => finish(resolve, s),
              (e) => finish(reject, e))
          } else if (code === DisconnectReason.loggedOut) {
            finish(reject, new Error(
              `Logged out by WhatsApp. Delete ${AUTH_DIR} and pair again with \`login\`.`))
          } else {
            finish(reject, new Error(`Connection closed before it opened (code ${code}).`))
          }
        }
      })
    })().catch((e) => finish(reject, e))
  })
}

async function runSend() {
  const raw = await readStdin()
  let payload
  try {
    payload = JSON.parse(raw || '{}')
  } catch (_) {
    throw new Error('send expects a JSON {jid, messages} object on stdin.')
  }
  const { jid, messages } = payload
  if (!jid || typeof jid !== 'string') throw new Error('Missing "jid" (the group JID).')
  if (!Array.isArray(messages) || messages.length === 0) throw new Error('No "messages" to send.')

  const { sock } = await openSocket({ allowQR: false })
  for (const text of messages) {
    await sock.sendMessage(jid, { text: String(text) })
  }
  await delay(1500) // let the server ack the last message before we disconnect
  try { sock.end() } catch (_) {}
  process.exit(0)
}

async function runGroups() {
  const { sock } = await openSocket({ allowQR: false })
  const groups = await sock.groupFetchAllParticipating()
  const list = Object.values(groups)
    .map((g) => ({ jid: g.id, name: g.subject }))
    .sort((a, b) => String(a.name).localeCompare(String(b.name)))
  process.stdout.write(JSON.stringify(list, null, 2) + '\n')
  try { sock.end() } catch (_) {}
  process.exit(0)
}

async function runLogin() {
  const qrcode = require('qrcode-terminal')
  process.stderr.write('Open WhatsApp > Linked devices > Link a device, then scan:\n')
  const { sock } = await openSocket({
    allowQR: true,
    timeoutMs: 120000,  // generous: the timer resets each time a fresh QR is shown
    onQR: (qr) => qrcode.generate(qr, { small: true }),
  })
  process.stdout.write(`Paired and connected. Credentials saved to ${AUTH_DIR}\n`)
  await delay(1000)
  try { sock.end() } catch (_) {}
  process.exit(0)
}

const COMMANDS = { send: runSend, groups: runGroups, login: runLogin }

const cmd = process.argv[2] || 'send'
const run = COMMANDS[cmd]
if (!run) {
  process.stderr.write(`Unknown command '${cmd}'. Use one of: ${Object.keys(COMMANDS).join(', ')}.\n`)
  process.exit(2)
}
run().catch((err) => {
  process.stderr.write((err && err.message ? err.message : String(err)) + '\n')
  process.exit(1)
})
