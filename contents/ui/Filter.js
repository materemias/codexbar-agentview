.pragma library

// Type-to-filter matching for the Agents and History tabs.
//
// A query is words separated by spaces, in any order. A session matches when
// every word matches one of its fields or recent turns. A word matches a
// field as a substring, else as prefixes of consecutive words ("cmh" finds
// "Check MCP history", "autor" finds "Auto-Research"); a turn only as a
// substring, so long prose cannot satisfy a short word by chance.

// The query's distinct lowercase words.
function words(query) {
    var out = []
    var parts = String(query || "").toLowerCase().split(/\s+/)
    for (var i = 0; i < parts.length; i++) {
        if (parts[i] && out.indexOf(parts[i]) === -1) out.push(parts[i])
    }
    return out
}

function isWordChar(c) {
    return c.toLowerCase() !== c.toUpperCase() || (c >= "0" && c <= "9")
}

// Whether text[k] begins a word: after a non-word character or at a
// camelCase boundary.
function wordStart(text, k) {
    if (!isWordChar(text[k])) return false
    if (k === 0) return true
    var p = text[k - 1], c = text[k]
    if (!isWordChar(p)) return true
    return c !== c.toLowerCase() && p !== p.toUpperCase()
}

// Lowercase text with the same length, so positions in it index `text`:
// a character whose lowercase form is longer ("İ") is kept as it is.
function lower(text) {
    var hay = text.toLowerCase()
    if (hay.length === text.length) return hay
    var out = ""
    for (var i = 0; i < text.length; i++) {
        var c = text[i].toLowerCase()
        out += c.length === 1 ? c : text[i]
    }
    return out
}

// Sorted positions of lowercase `word` in text, or null. `initials` allows
// the consecutive-word-prefix form.
function positions(text, word, initials) {
    if (!text || !word) return null
    var hay = lower(text)
    var at = hay.indexOf(word)
    if (at !== -1) {
        var run = []
        for (var r = 0; r < word.length; r++) run.push(at + r)
        return run
    }
    if (!initials) return null
    function nextWord(from) {
        for (var k = from; k < text.length; k++) if (wordStart(text, k)) return k
        return -1
    }
    // Each matched character continues the run, or starts the word right
    // after it; a word skipped in between breaks the match. Failed
    // (start, word index) states are memoized.
    var failed = {}
    function walk(from, j) {
        if (j === word.length) return []
        var key = from + ":" + j
        if (failed[key]) return null
        var candidates = []
        if (j === 0) {
            for (var k = 0; k < hay.length; k++) if (wordStart(text, k)) candidates.push(k)
        } else {
            candidates.push(from)
            var next = nextWord(from)
            if (next > from) candidates.push(next)
        }
        for (var c = 0; c < candidates.length; c++) {
            var k2 = candidates[c]
            if (k2 >= hay.length || hay[k2] !== word[j]) continue
            var rest = walk(k2 + 1, j + 1)
            if (rest) return [k2].concat(rest)
        }
        failed[key] = true
        return null
    }
    return walk(0, 0)
}

// The words that match text, each with its positions, or null for none.
function hits(text, ws, initials) {
    var out = []
    for (var i = 0; i < ws.length; i++) {
        var run = positions(text, ws[i], initials)
        if (run) out.push({ word: ws[i], run: run })
    }
    return out.length ? out : null
}

// Every hit position, sorted.
function flat(found) {
    var out = []
    for (var i = 0; found && i < found.length; i++) out = out.concat(found[i].run)
    return out.sort(function(a, b) { return a - b })
}

// Whether every word matches one of `fields` (strings) or `turns` (strings).
function matchesAll(fields, turns, ws) {
    var low = turns.map(lower)
    for (var i = 0; i < ws.length; i++) {
        var ok = false
        for (var f = 0; !ok && f < fields.length; f++) ok = positions(fields[f], ws[i], true) !== null
        for (var t = 0; !ok && t < low.length; t++) ok = low[t].indexOf(ws[i]) !== -1
        if (!ok) return false
    }
    return true
}

function escape(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
}

// StyledText for text[start, end) with the positions highlighted.
function styled(text, positions, start, end) {
    var hit = {}
    for (var i = 0; i < positions.length; i++) hit[positions[i]] = true
    var out = ""
    var k = start
    while (k < end) {
        var on = hit[k] === true
        var e = k
        while (e < end && (hit[e] === true) === on) e++
        var part = escape(text.slice(k, e))
        out += on ? "<b><font color=\"#ffb300\">" + part + "</font></b>" : part
        k = e
    }
    return out
}

// StyledText around the `focus` hits (a window per contiguous piece of a
// hit, so a long word between initials is cut; overlapping windows merged,
// the text between them elided), every hit in `found` highlighted. The
// line is clamped to two lines, so the windows share one budget and shrink
// as the line holds more pieces.
function excerpt(text, found, focus) {
    var pieces = []
    focus.forEach(function(h) {
        var start = h.run[0]
        for (var i = 1; i <= h.run.length; i++) {
            if (i < h.run.length && h.run[i] === h.run[i - 1] + 1) continue
            pieces.push([start, h.run[i - 1]])
            start = h.run[i]
        }
    })
    var before = Math.floor(60 / pieces.length)
    var after = Math.floor(100 / pieces.length)
    var spans = pieces.map(function(p) {
        return [Math.max(0, p[0] - before), Math.min(text.length, p[1] + 1 + after)]
    }).sort(function(a, b) { return a[0] - b[0] })
    var merged = [spans[0]]
    for (var i = 1; i < spans.length; i++) {
        var last = merged[merged.length - 1]
        if (spans[i][0] <= last[1]) last[1] = Math.max(last[1], spans[i][1])
        else merged.push(spans[i])
    }
    var pos = flat(found)
    var out = merged[0][0] > 0 ? "… " : ""
    for (var m = 0; m < merged.length; m++) {
        if (m > 0) out += " … "
        out += styled(text, pos, merged[m][0], merged[m][1])
    }
    return out + (merged[merged.length - 1][1] < text.length ? " …" : "")
}

// StyledText lines that show where every word matched, beyond what the row
// already shows. `fields`: [{ name, label, text, shown }], `turns`:
// [{ user, text }], whitespace collapsed. A line for each matched field the
// row does not show, then the turns holding words nothing before showed
// (else the first matching turn when nothing else is shown). The prompt
// line is dropped when a chosen user turn repeats the prompt with the same
// words. Each line centers on the words no shown field or earlier line
// showed. tern/host.luau's filterSnippet follows the same rules.
function snippets(fields, turns, ws) {
    if (!ws.length) return []
    var seen = Object.create(null)
    var inView = Object.create(null)
    var parts = []
    for (var i = 0; i < fields.length; i++) {
        var f = fields[i]
        var found = hits(f.text, ws, true)
        if (!found) continue
        for (var w = 0; w < found.length; w++) {
            seen[found[w].word] = true
            if (f.shown) inView[found[w].word] = true
        }
        if (!f.shown) parts.push({ name: f.name, prefix: f.label + ": ", text: f.text, found: found })
    }
    var turnHits = turns.map(function(t) { return hits(t.text, ws, false) })
    var chosen = []
    for (var a = 0; a < turns.length; a++) {
        var h = turnHits[a]
        if (!h || !h.some(function(x) { return !seen[x.word] })) continue
        chosen.push(a)
        for (var s = 0; s < h.length; s++) seen[h[s].word] = true
    }
    if (!chosen.length && !parts.length) {
        for (var b = 0; b < turns.length; b++) {
            if (turnHits[b]) {
                chosen.push(b)
                break
            }
        }
    }
    parts = parts.filter(function(p) {
        return p.name !== "lastPrompt" || !chosen.some(function(c) {
            var t = turns[c]
            return t.user && t.text.slice(0, 100) === p.text.slice(0, 100)
                && p.found.every(function(x) {
                    return turnHits[c].some(function(y) { return y.word === x.word })
                })
        })
    }).concat(chosen.map(function(c) {
        return { prefix: turns[c].user ? "> " : "· ", text: turns[c].text, found: turnHits[c] }
    }))
    return parts.map(function(p) {
        var fresh = p.found.filter(function(x) { return !inView[x.word] })
        p.found.forEach(function(x) { inView[x.word] = true })
        return p.prefix + excerpt(p.text, p.found, fresh.length ? fresh : p.found)
    })
}
