.pragma library
.import "Usage.js" as Usage

function providerName(id) {
    var names = { claude: "Claude", codex: "Codex", zai: "z.ai",
        opencodego: "OpenCode Go", openrouter: "OpenRouter", kilo: "Kilo",
        typesafe: "TypeSafe", omp: "omp", pi: "Pi", opencode: "OpenCode" }
    return names[id] || "Coding agent"
}

function accountIdentity(record) {
    return typeof record.accountEmail === "string" && record.accountEmail.trim()
        ? record.accountEmail.trim().toLowerCase() : null
}

// A projection doesn't count before this much of the window has passed: the
// first prompt of a fresh 5h window, or a busy first day of a weekly one,
// projects exhaustion it rarely reaches.
var PROJECTION_MIN_ELAPSED_PERCENT = 25
// Providers report the same reset a few seconds or minutes apart between
// fetches; only a move larger than this is a new period.
var RESET_JITTER_MS = 15 * 60000
// A failed notification (no notify-send, Plasma refusing a burst) is tried
// again after this long, not on every poll.
var RETRY_MS = 10 * 60000

function samePeriod(old, reset) {
    if (!old) return false
    if (typeof old.reset === "number" && typeof reset === "number")
        return Math.abs(old.reset - reset) < RESET_JITTER_MS
    return old.reset === reset
}

function usage(previous, records, now, enabled) {
    var state = Object.assign({}, previous || {})
    var events = []
    var scopes = {}
    var counts = {}
    for (var a = 0; a < records.length; a++) {
        var account = records[a]
        if (!account || typeof account.id !== "string" || account.composite) continue
        if (typeof account.sourceScope === "string" && /^[a-f0-9]{64}$/.test(account.sourceScope))
            scopes[JSON.stringify([account.id, account.sourceScope])] = true
        var providerKey = JSON.stringify(account.id)
        counts[providerKey] = (counts[providerKey] || 0) + 1
    }
    for (var priorKey in state)
        if (!scopes[state[priorKey].source]) delete state[priorKey]
    if (!enabled) return { state: state, events: events }
    for (var p = 0; p < records.length; p++) {
        var record = records[p]
        if (!record || record.ok !== true || record.stale === true || record.composite
                || typeof record.id !== "string") continue
        var source = JSON.stringify([record.id, record.sourceScope])
        if (!scopes[source]) continue
        var provider = JSON.stringify(record.id)
        var accountId = accountIdentity(record)
        // Without an identity, two accounts cannot be safely distinguished.
        if (accountId === null && counts[provider] > 1) continue
        var label = providerName(record.id) + (accountId !== null ? " · " + accountId : "")
        var windows = []
        var slots = ["primary", "secondary", "tertiary"]
        for (var s = 0; s < slots.length; s++)
            windows.push({ slot: slots[s], window: record[slots[s]] })
        var extras = Array.isArray(record.extraRateWindows) ? record.extraRateWindows : []
        for (var e = 0; e < extras.length; e++) {
            var extra = extras[e]
            if (extra && typeof extra.id === "string" && extra.id.length > 0)
                windows.push({ slot: extra.id, window: extra.window })
        }
        for (var w = 0; w < windows.length; w++) {
            var entry = windows[w]
            var win = entry.window
            if (!win || typeof win.usedPercent !== "number"
                    || !isFinite(win.usedPercent) || win.usedPercent < 0) continue
            var reset = win.resetsAt ? new Date(win.resetsAt).getTime() : null
            if (reset !== null && (!isFinite(reset) || reset <= now)) continue
            // OpenRouter's normalized primary tracks keyUsageMonthly, but its
            // response has no reset timestamp. Its period is the UTC month.
            if (reset === null && record.id === "openrouter" && entry.slot === "primary") {
                var month = new Date(now)
                reset = month.getUTCFullYear() + "-" + month.getUTCMonth()
            }
            var key = JSON.stringify([record.id, record.sourceScope, accountId, entry.slot])
            var old = state[key]
            var level = win.usedPercent >= 95 ? 3 : win.usedPercent >= 80 ? 2
                : Usage.pacePercent(win, now) >= PROJECTION_MIN_ELAPSED_PERCENT
                    && Usage.projectedPercent(win, now) > 100 ? 1 : 0
            var same = samePeriod(old, reset)
            var priorLevel = same ? old.level : 0
            var pending = same ? old.pending : []
            var retryAt = same ? old.retryAt || 0 : 0
            var claimedLevel = Math.max.apply(Math, [priorLevel].concat(pending))
            // Unknown reset dates rearm only after observing fresh usage below 80%.
            var cycle = same ? old.cycle : {}
            if (reset === null && level === 0 && claimedLevel >= 2) {
                priorLevel = 0
                pending = []
                cycle = {}
                claimedLevel = 0
                retryAt = 0
            }
            state[key] = { source: source, reset: reset, cycle: cycle,
                level: priorLevel, pending: pending, retryAt: retryAt }
            if (level <= claimedLevel || now < retryAt) continue
            state[key].pending = pending.concat([level])
            var detail = level === 1 ? "Projected to exhaust before reset at the current pace."
                : Math.round(win.usedPercent) + "% of the allowance used."
            events.push({ kind: "usage", usageKey: key, cycle: cycle, level: level,
                title: "CodexBar AgentView " + (level === 3 ? "usage critical" : "usage warning"),
                // Never pass provider-supplied titles or unknown slot IDs into notification text.
                body: label + " · " + (Usage.windowLabel(record.id, entry.slot, win)
                    || "Usage window") + ": " + detail })
        }
    }
    return { state: state, events: events }
}

function delivery(previous, event, succeeded, now) {
    var old = event && previous[event.usageKey]
    // The in-memory cycle token also rejects callbacks after observed resets or scope retirement.
    if (!old || old.cycle !== event.cycle || old.pending.indexOf(event.level) < 0)
        return previous
    var state = Object.assign({}, previous)
    state[event.usageKey] = {
        source: old.source, reset: old.reset, cycle: old.cycle,
        level: succeeded ? Math.max(old.level, event.level) : old.level,
        pending: old.pending.filter(function(level) { return level !== event.level }),
        retryAt: succeeded ? old.retryAt || 0 : (now || Date.now()) + RETRY_MS
    }
    return state
}

function ompModeLabel(status) {
    if (!status || ["FILL", "TARGET", "EXPIRY-BURN", "BURN", "NO-BANK", "IDLE"]
            .indexOf(status.mode) < 0
            || ["auto", "confirm"].indexOf(status.operatingMode) < 0
            || typeof status.stalled !== "boolean") return null
    return status.operatingMode + " · "
        + (status.stalled ? "STALLED (" + status.mode + ")" : status.mode)
}

function ompMode(previous, status, enabled) {
    var old = previous || { mode: null, sequence: 0, transition: null, pending: false }
    var mode = enabled ? ompModeLabel(status) : null
    var state = { mode: mode, sequence: old.sequence, transition: null, pending: false }
    var events = []
    // Unavailable, disabled, and first observations establish a silent baseline.
    if (mode === null || old.mode === null) return { state: state, events: events }
    if (mode !== old.mode) {
        state.sequence++
        state.transition = { kind: "ompMode",
            sequence: state.sequence, title: "CodexBar AgentView omp mode changed",
            body: old.mode + " → " + mode }
    } else {
        state.transition = old.transition
        state.pending = old.pending
    }
    if (state.transition && !state.pending) {
        state.pending = true
        events.push(state.transition)
    }
    return { state: state, events: events }
}

function ompModeDelivery(previous, event, succeeded) {
    if (!previous || !previous.transition
            || previous.transition.sequence !== event.sequence) return previous
    return { mode: previous.mode, sequence: previous.sequence,
        transition: succeeded ? null : previous.transition, pending: false }
}

function agents(previous, records, enabled) {
    var old = previous || { enabled: false, sessions: {} }
    var sessions = {}
    var events = []
    for (var i = 0; i < records.length; i++) {
        var record = records[i]
        if (!record || typeof record.provider !== "string"
                || typeof record.sessionId !== "string" || !record.sessionId.trim()
                || record.sessionId.indexOf("untracked-") === 0
                || ["working", "blocked", "idle"].indexOf(record.state) < 0) continue
        var key = JSON.stringify([record.provider, record.sessionId])
        if (sessions[key] !== undefined) continue
        sessions[key] = record.state
        if (enabled && old.enabled && old.sessions[key] !== undefined
                && old.sessions[key] !== "blocked" && record.state === "blocked") {
            events.push({ kind: "agent",
                title: "CodexBar AgentView agent waiting for input",
                body: providerName(record.provider) + " is waiting for your input." })
        }
    }
    return { state: { enabled: enabled, sessions: sessions }, events: events }
}
