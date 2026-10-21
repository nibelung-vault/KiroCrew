/** Pure rules for the terminal's find and fullscreen affordances (the layout
 *  rules and the Cmd/Ctrl+[Shift+]F key match), kept out of the components so
 *  they unit-test without rendering. */

/** Whether a keydown is the F of the find/fullscreen chord. Matches the letter
 *  TYPED (`key`), so on a remapped Latin layout such as Dvorak, Ctrl+U on
 *  QWERTY's F position still reaches the shell as kill-line, and the key that
 *  types `f` opens find. Falls back to the physical position (`code === 'KeyF'`)
 *  only when the typed key is not a Latin letter, so non-Latin layouts (and
 *  macOS Option glyphs) keep a working chord. */
export function isFindChordKey(key: string, code: string): boolean {
  const k = key.toLowerCase()
  if (k.length === 1 && k >= 'a' && k <= 'z') return k === 'f'
  return code === 'KeyF'
}

/** Whether the dock terminal may go fullscreen. Fullscreen fills the dashboard
 *  content area, whose stacking context only exists on desktop; on a mobile
 *  viewport the shell's nav drawer and scrims must paint over the content, so a
 *  maximized terminal there would cover them. Desktop only. */
export function isTerminalFullscreenAllowed(isMobile: boolean): boolean {
  return !isMobile
}

/** The stacking-context class `<main>` carries. `isolate` ONLY when the dock
 *  terminal is actually rendered AND maximized — the maximized panel is a later
 *  sibling of `<main>` and must paint above the in-content chat layers it covers.
 *  It tracks the dock's own render condition (`terminalEnabled && !terminalPoppedOut`),
 *  not the bare fullscreen flag: the flag persists across a pop-out and a disable,
 *  but no panel is on screen then, so isolating `<main>` would trap a page-level
 *  inline modal inside it for nothing. Docked-but-not-maximized, popped out,
 *  disabled, or closed: no stacking context, so the modal paints over the dock. */
export function mainStackingClass(opts: {
  isTerminalFullscreen: boolean
  terminalEnabled: boolean
  terminalPoppedOut: boolean
}): string {
  const maximizedPanelOnScreen =
    opts.isTerminalFullscreen && opts.terminalEnabled && !opts.terminalPoppedOut
  return maximizedPanelOnScreen ? 'isolate' : ''
}
