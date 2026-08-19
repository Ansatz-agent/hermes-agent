/** Reference apps. Importing this module registers them (defineWidgetApp
 *  runs at module load) — appLayout imports it once at startup. User widgets
 *  from $HERMES_HOME/tui-widgets ride the same import (async, non-fatal). */
import { loadUserWidgets, watchUserWidgets } from '../userWidgets.js'

void loadUserWidgets()

// Vitest resets module graphs within long-lived workers. Starting the
// production hot-loader on every reset leaks native FSEvent watchers and can
// exhaust macOS's per-process watcher allowance before the suite completes.
if (!process.env.VITEST && process.env.NODE_ENV !== 'test') {
  watchUserWidgets()
}

export { dialogTestApp } from './dialogTest.js'
export { gridTestApp } from './gridTest.js'
export { GRID_STREAM_COUNT, type GridTestState } from './gridTestState.js'
export { tickerApp, type TickerState } from './ticker.js'
export { weatherApp, type WeatherState } from './weather.js'
