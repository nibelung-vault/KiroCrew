/**
 * Screenshot harness for the terminal find bar and dock fullscreen (#10176, #9906).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with every /api/** call answered from fixtures via Playwright route
 * interception — no gateway, no PTY. The terminal WebSocket is a scripted PTY
 * that prints a short build log (the word "warning" appears on three lines, so
 * the find has several matches to step through) and then a prompt.
 *
 * Every state is reached through the production key path, not by poking state:
 * the find bar opens on the find chord pressed in the focused terminal (Ctrl+F
 * on this Linux runner; the same handler answers Cmd+F on macOS), the query is
 * typed into the bar, Enter / Shift+Enter step the matches, and fullscreen is
 * the Ctrl+Shift+F chord (Cmd+Shift+F on macOS). Each frame asserts the DOM
 * state first — the "n of m" counter text, the "No results" marker, the
 * fullscreen panel's `absolute inset-0` class and `<main>`'s `isolate` — so a
 * screenshot that passes is a picture of the state it names.
 *
 * Captured, each in dark and light (theme via localStorage `mc-theme`):
 *   01-find-match-<theme>.png      find bar open, "1 of 3", current match highlighted
 *   02-find-step-<theme>.png       after Enter: counter moved to "2 of 3"
 *                                  (Shift+Enter is asserted to step back)
 *   03-find-no-match-<theme>.png   a query with no hit: "No results"
 *   04-fullscreen-<theme>.png      Ctrl/Cmd+Shift+F: the dock fills the content area
 *
 * Usage: node scripts/capture-terminal-find.mjs [outDir]
 *   (default outDir: website/temp-screenshots/terminal-find, gitignored)
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || fileURLToPath(new URL('../temp-screenshots/terminal-find/', import.meta.url))
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 1280, height: 800 }
const PROMPT = '\x1b[1;32m$\x1b[0m '
/** Scrollback with three "warning" lines, so the find has matches to step. */
const LOG = [
  '$ npm run build',
  '',
  '> website@0.0.0 build',
  '> tsc -b && vite build',
  '',
  'vite v6.3.5 building for production...',
  '\x1b[33mwarning\x1b[0m: src/App.tsx (412): unused import "Loader2"',
  'transforming (1830) src/components/CliPanel.tsx',
  '\x1b[33mwarning\x1b[0m: chunk index-BzK2.js is larger than 500 kB after minification',
  'rendering chunks (214)...',
  'computing gzip size (792)...',
  '\x1b[33mwarning\x1b[0m: 2 dynamic imports could not be resolved',
  'dist/index.html                    1.92 kB │ gzip:   0.81 kB',
  'dist/assets/index-BzK2.js        612.40 kB │ gzip: 188.11 kB',
  '\x1b[32m✓ built in 6.26s\x1b[0m',
  '',
].join('\r\n')

async function stubContext(context, theme) {
  await stubDashboardApi(context, {
    theme,
    // Seed the docked terminal open with one tab (the persisted shape a reload
    // restores); passed here because the stub's own init script clears storage.
    localStorageEntries: {
      'mc-bottom-terminal': JSON.stringify({
        open: true, height: 340, position: 'bottom',
        tabs: [{ id: 'fixture-tab-1' }], activeId: 'fixture-tab-1',
      }),
    },
    extra: async (path, route) => {
      // Create-slot must return a keyed slot object (a keyless one crashes the shell).
      if (path === '/api/chat/slots' && route.request().method() === 'POST') {
        await json(route, { key: 'fixture-chat', title: 'New Session…', agent: 'kirocrew' })
        return true
      }
      if (path.startsWith('/api/chat/slots/')) {
        await json(route, {})
        return true
      }
      return false
    },
  })
  // `open` is per window (sessionStorage).
  await context.addInitScript(() => sessionStorage.setItem('mc-bottom-terminal-open', '1'))
  // Registered AFTER the shared stub (which routes /api/ws): the last match wins.
  await context.routeWebSocket(/\/api\/ws\/terminal\//, ws => {
    ws.send(JSON.stringify({ type: 'ready' }))
    ws.send(Buffer.from(LOG + PROMPT))
    ws.onMessage(m => {
      if (typeof m === 'string') return // resize/control frames
      ws.send(Buffer.from(m.toString('utf8').replace(/\r/g, '\r\n')))
    })
  })
}

async function openPage(browser, base, theme) {
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 })
  await stubContext(context, theme)
  const page = await context.newPage()
  logPageProblems(page)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  const panel = page.getByTestId('bottom-terminal-panel')
  await panel.waitFor({ state: 'visible', timeout: 20000 })
  // Wait for the scripted log to land in the xterm buffer.
  await page.waitForFunction(() => {
    const rows = document.querySelector('[data-testid="bottom-terminal-panel"] .xterm-rows')
    return !!rows && rows.textContent.includes('built in')
  }, null, { timeout: 20000 })
  await page.waitForTimeout(400) // slide + fit settle
  return { context, page, panel }
}

async function expectCount(page, re, what) {
  const count = page.getByTestId('terminal-find-count')
  await page.waitForFunction(
    ([src, flags]) => new RegExp(src, flags).test(document.querySelector('[data-testid="terminal-find-count"]')?.textContent ?? ''),
    [re.source, re.flags], { timeout: 5000 },
  ).catch(() => {})
  const text = (await count.textContent()) ?? ''
  if (!re.test(text)) throw new Error(`${what}: find counter read ${JSON.stringify(text)}, expected ${re}`)
  return text
}

/** Clip to the docked terminal panel, which holds the find bar and the scrollback. */
async function shotPanel(page, panel, name) {
  await panel.scrollIntoViewIfNeeded()
  const box = await panel.boundingBox()
  if (!box) throw new Error('terminal panel not laid out')
  await page.screenshot({ path: `${OUT}/${name}.png`, clip: box })
}

async function captureTheme(browser, base, theme) {
  // ---- Find: open, step, no-match ----
  const { context, page, panel } = await openPage(browser, base, theme)
  await panel.locator('.xterm-helper-textarea').first().focus()
  await page.keyboard.press('Control+f')
  const bar = page.getByTestId('terminal-find-bar')
  await bar.waitFor({ state: 'visible', timeout: 5000 })
  const input = bar.getByRole('textbox', { name: 'Search terminal scrollback' })
  // The hook focuses the input on the next frame, once it has mounted.
  const focused = await input.evaluate(el => new Promise(resolve => {
    const t0 = performance.now()
    const tick = () => (el === document.activeElement ? resolve(true)
      : performance.now() - t0 > 3000 ? resolve(false) : requestAnimationFrame(tick))
    tick()
  }))
  if (!focused) {
    throw new Error('find chord opened the bar but did not focus its input')
  }
  await input.type('warning')
  const first = await expectCount(page, /^[123] of 3$/, 'open')
  // The highlight decorations are painted into the xterm decoration layer, a
  // frame or two after the count reports.
  const painted = await page.waitForFunction(
    () => document.querySelectorAll('[data-testid="bottom-terminal-panel"] .xterm-decoration').length > 0,
    null, { timeout: 5000 },
  ).then(() => true, () => false)
  if (!painted) throw new Error('no match highlight decorations painted')
  await shotPanel(page, panel, `01-find-match-${theme}`)

  await input.press('Enter')
  const stepped = await expectCount(page, /^[123] of 3$/, 'after Enter')
  if (stepped === first) throw new Error(`Enter did not move the current match (still ${stepped})`)
  await shotPanel(page, panel, `02-find-step-${theme}`)
  await input.press('Shift+Enter')
  const back = await expectCount(page, /^[123] of 3$/, 'after Shift+Enter')
  if (back !== first) throw new Error(`Shift+Enter did not step back (${back}, expected ${first})`)
  await input.press('Enter') // leave it on the stepped match; not shot

  await input.fill('segfault')
  await expectCount(page, /^No results$/, 'no-match')
  await shotPanel(page, panel, `03-find-no-match-${theme}`)

  // ---- Fullscreen: close find (Escape returns focus to the shell), then press
  //      the chord in the focused terminal, so the frame shows a plain
  //      maximized shell. ----
  await input.press('Escape')
  await bar.waitFor({ state: 'detached', timeout: 5000 })
  await panel.locator('.xterm-helper-textarea').first().focus()
  await page.keyboard.press('Control+Shift+f')
  await page.waitForFunction(() => {
    const p = document.querySelector('[data-testid="bottom-terminal-panel"]')
    const main = document.getElementById('main-content')
    return !!p && p.classList.contains('inset-0') && p.classList.contains('absolute') && !!main && main.classList.contains('isolate')
  }, null, { timeout: 5000 }).catch(() => {})
  const state = await page.evaluate(() => {
    const p = document.querySelector('[data-testid="bottom-terminal-panel"]')
    const main = document.getElementById('main-content')
    return {
      panelClass: p?.className ?? '',
      mainIsolate: !!main?.classList.contains('isolate'),
      stored: sessionStorage.getItem('mc-bottom-terminal-fullscreen'),
    }
  })
  if (!/\babsolute\b/.test(state.panelClass) || !/\binset-0\b/.test(state.panelClass) || !state.mainIsolate || state.stored !== '1') {
    throw new Error('fullscreen chord did not maximize the dock: ' + JSON.stringify(state))
  }
  await page.waitForTimeout(400) // refit after the size change
  const pbox = await panel.boundingBox()
  if (!pbox || pbox.height < VIEWPORT.height * 0.8) {
    throw new Error('fullscreen panel does not fill the content area: ' + JSON.stringify(pbox))
  }
  // The whole window, so the frame shows the panel covering the content area
  // under the top bar and beside the nav rail.
  await page.screenshot({ path: `${OUT}/04-fullscreen-${theme}.png` })
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const theme of ['dark', 'light']) await captureTheme(browser, base, theme)
  } finally {
    await browser.close()
    srv.close()
  }
  console.log(`wrote 8 screenshots to ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
