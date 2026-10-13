-- Host half of the CodexBar AgentView plugin: one poller per daemon, dashboard and settings.
--
-- Data comes from the KDE applet's helpers next to this folder
-- (../contents/scripts): codexbar_fetch.py is the provider normalization path
-- and codexbar_agents.py writes agents.json under $XDG_STATE_HOME/codexbar-agentview
-- (default ~/.local/state/codexbar-agentview). While the applet runs,
-- its data is fresh and reused; the plugin only runs a helper when the data it
-- would read is older than the applet's own cadence.

local ui = tern.ui

local HOME = tern.getenv("HOME") or ""
local SCRIPTS = tern.plugin.dir .. "/../contents/scripts"
local XDG_STATE = tern.getenv("XDG_STATE_HOME") or ""
local STATE_DIR = (if string.sub(XDG_STATE, 1, 1) == "/" then XDG_STATE else HOME .. "/.local/state") .. "/codexbar-agentview"
local AGENTS_FILE = STATE_DIR .. "/agents.json"

-- One-shot: carry settings over from the plugin's former id `codexbar`.
if tern.kv.get("settings") == nil then
	local ok, raw = pcall(tern.fs.read, tern.plugin.data .. "/../codexbar/kv.json", 1024 * 1024)
	local okj, old = pcall(tern.json.decode, if ok then raw else "")
	if okj and type(old) == "table" and type(old.settings) == "table" then
		tern.kv.set("settings", old.settings)
	end
end
local FORECAST_URL = "https://codex-reset.com/api/forecast"
local PANE_TAG = 2 ^ 44 -- the window API cuts pane ids to their low 44 bits

local TICK_MS = 500
local AGENTS_STALE_MS = 15000 -- the applet sweeps every 5 s
local CACHE_EVERY_S = 10

local PROVIDER_ORDER = { "claude", "codex", "zai", "opencodego", "openrouter", "kilo", "typesafe" }
local PROVIDER_NAMES = {
	claude = "Claude Code",
	codex = "OpenAI Codex",
	zai = "z.ai",
	opencodego = "OpenCode Go",
	openrouter = "OpenRouter",
	kilo = "Kilo",
	typesafe = "TypeSafe",
}
local PROVIDER_ICONS = { claude = "app-claude", codex = "app-codex" }
local AGENT_ICONS = { claude = "app-claude", codex = "app-codex", omp = "app-omp", pi = "pi-mark" }
local CODEX_PLAN_WEIGHT = { plus = 1, pro = 20 }
local TABS = { "usage", "agents", "history" }
local TAB_NAMES = { usage = "Usage", agents = "Agents", history = "History" }

-- Settings ------------------------------------------------------------------
-- `tern.kv` key `settings`, a table; only this host half writes settings.

local DEFAULTS = {
	providers = { "claude", "codex", "zai", "opencodego", "openrouter", "kilo" },
	cliPath = "",
	refreshSeconds = 300,
	historyLimit = 100,
	forecast = true,
	composite5h = false,
	composite7d = true,
	recentUsage = true,
	includeUntracked = true,
	includeNonTern = false,
}

local NUMBER_LIMITS = { refreshSeconds = { 60, 3600 }, historyLimit = { 10, 1000 } }

-- Parse both persisted configuration and prefs events at their boundary.
local function parseSetting(name: string, value: any): any
	if name == "providers" then
		if type(value) ~= "table" then
			return nil
		end
		local enabled = {}
		local count = 0
		for k, p in value do
			if type(k) ~= "number" or k % 1 ~= 0 or k < 1 or k > #value
				or type(p) ~= "string" or not table.find(PROVIDER_ORDER, p) then
				return nil
			end
			enabled[p] = true
			count += 1
		end
		if count ~= #value then
			return nil
		end
		local ordered = tern.json.array()
		for _, p in PROVIDER_ORDER do
			if enabled[p] then
				table.insert(ordered, p)
			end
		end
		return ordered
	elseif name == "cliPath" then
		if type(value) == "string" and utf8.len(value) ~= nil and not string.find(value, "[%z\r\n]") then
			return value
		end
	elseif NUMBER_LIMITS[name] then
		local limits = NUMBER_LIMITS[name]
		if type(value) == "number" and value == value and value % 1 == 0
			and value >= limits[1] and value <= limits[2] then
			return value
		end
	elseif type(DEFAULTS[name]) == "boolean" and type(value) == "boolean" then
		return value
	end
	return nil
end

local function setting(name: string): any
	local s = tern.kv.get("settings")
	local parsed = if type(s) == "table" then parseSetting(name, s[name]) else nil
	return if parsed ~= nil then parsed else DEFAULTS[name]
end

local function cliPath(): string
	local configured = setting("cliPath")
	if type(configured) == "string" and configured ~= "" then
		return (string.gsub(configured, "^~", HOME))
	end
	for _, p in {
		HOME .. "/.local/bin/codexbar",
		"/usr/bin/codexbar",
		"/usr/local/bin/codexbar",
		"/opt/homebrew/bin/codexbar",
	} do
		if tern.fs.exists(p) then
			return p
		end
	end
	return "codexbar"
end

-- Small helpers -------------------------------------------------------------

local function val(x: any): any
	if x == tern.json.null then
		return nil
	end
	return x
end

local function str(x: any): string
	x = val(x)
	if x == nil then
		return ""
	end
	return tostring(x)
end

local function nowMs(): number
	return os.time() * 1000
end

-- Seconds since the epoch of an ISO 8601 UTC time.
local function epoch(iso: any): number?
	if type(iso) ~= "string" then
		return nil
	end
	local y, m, d, hh, mm, ss = string.match(iso, "^(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)")
	if not y then
		return nil
	end
	local year, month, day = tonumber(y) :: number, tonumber(m) :: number, tonumber(d) :: number
	if month <= 2 then
		year -= 1
	end
	local era = year // 400
	local yoe = year - era * 400
	local doy = (153 * (if month > 2 then month - 3 else month + 9) + 2) // 5 + day - 1
	local doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
	return (era * 146097 + doe - 719468) * 86400
		+ (tonumber(hh) :: number) * 3600
		+ (tonumber(mm) :: number) * 60
		+ (tonumber(ss) :: number)
end

local MONTHS = { "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec" }

local function monthDay(t: number): string
	local d = os.date("*t", t)
	return MONTHS[d.month] .. " " .. d.day
end

-- "16:00" today, else "Oct 9, 16:00".
local function absoluteTime(t: number, now: number): string
	local hhmm = os.date("%H:%M", t)
	if os.date("%Y-%m-%d", t) == os.date("%Y-%m-%d", now) then
		return hhmm
	end
	return monthDay(t) .. ", " .. hhmm
end

-- "4d 9h", "2h 28m", "12m".
local function span(secs: number): string
	if secs <= 0 then
		return "soon"
	end
	local mins = secs // 60
	local hrs = mins // 60
	if hrs >= 24 then
		return (hrs // 24) .. "d " .. (hrs % 24) .. "h"
	end
	if hrs > 0 then
		return hrs .. "h " .. (mins % 60) .. "m"
	end
	return mins .. "m"
end

-- Largest unit only: "15s", "7m", "23h", "4d".
local function ageFrom(ms: any, now: number): string
	ms = tonumber(val(ms))
	if not ms or ms <= 0 then
		return ""
	end
	local secs = math.max(0, now - ms) // 1000
	if secs < 60 then
		return secs .. "s"
	end
	local mins = secs // 60
	if mins < 60 then
		return mins .. "m"
	end
	local hrs = mins // 60
	if hrs < 24 then
		return hrs .. "h"
	end
	return (hrs // 24) .. "d"
end

local function ageToken(ms: any, now: number, fresh: number, recent: number): string
	ms = tonumber(val(ms))
	if not ms then
		return "dim"
	end
	local age = now - ms
	if age < fresh then
		return "num"
	end
	return if age < recent then "muted" else "dim"
end

local function cwdLabel(path: any): string
	local p = str(path)
	return string.match(p, "([^/]+)/*$") or p
end

local function shortPath(path: any): string
	local p = str(path)
	if HOME ~= "" and string.sub(p, 1, #HOME) == HOME then
		return "~" .. string.sub(p, #HOME + 1)
	end
	return p
end

local function oneLine(text: any): string
	return (string.gsub(str(text), "%s+", " "))
end

local function clip(text: string, n: number): string
	if (utf8.len(text) or #text) <= n then
		return text
	end
	return string.sub(text, 1, (utf8.offset(text, n) or (n + 1)) - 1) .. "…"
end

local function pad(text: string, n: number): string
	local len = utf8.len(text) or #text
	if len >= n then
		return clip(text, n)
	end
	return text .. string.rep(" ", n - len)
end

local function uriEncode(s: string): string
	return (string.gsub(s, "[^%w%-_%.~]", function(c)
		return string.format("%%%02X", string.byte(c))
	end))
end

local function nodeKey(s: string): string
	return (string.gsub(s, "[^%w%-_]", "_"))
end

local function withp(n: Node, props: { [string]: any }): Node
	local p: { [string]: any } = n.p or {}
	for k, v in props do
		p[k] = v
	end
	n.p = p
	return n
end

local function button(text: string, act: string, tone: string?): Node
	return ui.el("button", { class = "cb-btn" .. (if tone then " " .. tone else ""), text = text, actions = { click = act } })
end

-- Store: one per daemon, shared by every open dashboard -----------------------

local store = {
	usage = nil :: any,
	usageAt = 0,
	fetching = false,
	cacheReadAt = 0,
	usageGeneration = 0,
	usageError = nil :: string?,
	agents = nil :: any,
	agentsRaw = "",
	scanning = false,
	summarySig = "",
	openSig = "",
	lastGoto = nil :: any,
}

type Block = {
	cx: BlockCx,
	tab: string,
	sel: string?,
	peek: boolean,
	filter: string,
	launched: { [string]: number },
	busy: string?,
	closed: boolean?,
}

local blocks: { [number]: Block } = {}

type SettingsBlock = {
	cx: BlockCx,
	page: string,
	query: string,
	cursor: number,
	searching: boolean,
	focus: string?,
	editing: { row: string, draft: string, cursor: number }?,
}
local settingsBlocks: { [number]: SettingsBlock } = {}

local function renderAll()
	for _, b in blocks do
		b.cx:render()
	end
	for _, b in settingsBlocks do
		b.cx:render()
	end
end

local function python(script: string, args: { string }, timeout: number, done: (ProcessResult) -> ())
	local argv = { "python3", SCRIPTS .. "/" .. script }
	for _, a in args do
		table.insert(argv, a)
	end
	local ok, err = pcall(tern.process.run, argv, { timeout_ms = timeout }, function(res: ProcessResult)
		done(res)
	end)
	if not ok then
		done({ status = -1, stdout = "", stderr = tostring(err), timed_out = false })
	end
end

local function firstLine(text: string): string
	return string.match(text, "[^\n]+") or ""
end

-- Usage math, ported from Usage.js and main.qml -----------------------------

local function windowSpan(w: any): number
	local a, b = epoch(w.startsAt), epoch(w.resetsAt)
	if a and b then
		return b - a
	end
	return (tonumber(val(w.windowMinutes)) or 0) * 60
end

-- Elapsed share of the window (0–100), or -1 without a usable reset.
local function pacePercent(w: any, now: number): number
	local reset = epoch(w.resetsAt)
	local total = windowSpan(w)
	if not reset or total <= 0 then
		return -1
	end
	local remaining = reset - now
	if remaining <= 0 or remaining > total then
		return -1
	end
	return (1 - remaining / total) * 100
end

-- "ok", "yellow", "orange" or "red", by pace when it is settled (>= 3%).
local function level(pct: number, pace: number): string
	if pace >= 3 then
		if pct <= pace then
			return "ok"
		end
		local t = (pct - pace) / (100 - pace)
		return if t >= 2 / 3 then "red" elseif t >= 1 / 3 then "orange" else "yellow"
	end
	return if pct >= 90 then "red" elseif pct >= 70 then "orange" elseif pct >= 50 then "yellow" else "ok"
end

local LEVEL_RANK = { ok = 0, yellow = 1, orange = 2, red = 3 }
local LEVEL_TONE = { ok = "success", yellow = "warning", orange = "warning", red = "error" }
local STATUS_TONE = { ok = "ok", yellow = "warn", orange = "warn", red = "error" }

local function worse(a: string?, b: string?): string?
	if not a then
		return b
	end
	if not b then
		return a
	end
	return if LEVEL_RANK[b] > LEVEL_RANK[a] then b else a
end

local function windowLabel(provider: string, slot: string, w: any, extra: string): string
	if provider == "codex" and (slot == "codex-base-model-inference" or slot == "gpt-reserve" or extra == "gpt-reserve") then
		return "Reserve 7d"
	end
	if extra ~= "" then
		return extra
	end
	if slot == "claude-design" then
		return "Design"
	elseif slot == "claude-routines" then
		return "Routines"
	elseif provider == "claude" and slot == "tertiary" then
		return "Sonnet"
	elseif provider == "openrouter" then
		return "Limit"
	elseif provider == "kilo" then
		return "Credits"
	elseif provider == "zai" and slot == "secondary" then
		return "Monthly"
	end
	local minutes = tonumber(val(w.windowMinutes))
	if minutes == 300 then
		return "5h"
	elseif minutes == 1440 then
		return "1d"
	elseif minutes == 10080 then
		return "7d"
	elseif minutes == 43200 then
		return "Monthly"
	end
	return slot
end

local function formatReset(w: any, now: number): string
	local reset = epoch(w.resetsAt)
	if reset then
		if reset <= now then
			return "soon"
		end
		return absoluteTime(reset, now) .. " (" .. span(reset - now) .. ")"
	end
	local desc = str(w.resetDescription)
	desc = string.gsub(desc, "^[Rr]esets%s+", "")
	desc = string.gsub(desc, "%s*%b()%s*$", "")
	return desc
end

local function leftUntil(t: number, now: number): string
	local diff = t - now
	if diff <= 0 then
		return "soon"
	elseif diff < 3600 then
		return math.ceil(diff / 60) .. "m"
	elseif diff < 86400 then
		return math.ceil(diff / 3600) .. "h"
	end
	return (string.gsub(span(diff), " 0h$", ""))
end

local function resetCredits(credits: any, now: number): string
	credits = val(credits)
	if type(credits) ~= "table" then
		return ""
	end
	local count = tonumber(val(credits.count))
	if not count or count < 1 then
		return ""
	end
	local text = count .. " saved reset" .. (if count == 1 then "" else "s")
	local exp = epoch(credits.soonestExpiresAt)
	if exp then
		text ..= " · soonest expires " .. (if exp <= now then "now" else "in " .. leftUntil(exp, now))
	end
	return text
end

-- Pooled windows across Codex accounts, in Plus-sized allowances (main.qml compositeStats).
local function composite(records: { any }, minutes: number, now: number): any
	local windows = {}
	local consumed, hours = 0, 0
	for _, r in records do
		local w = nil
		for _, slot in { "primary", "secondary" } do
			local c = val(r[slot])
			if type(c) == "table" and tonumber(val(c.windowMinutes)) == minutes and val(c.usedPercent) ~= nil then
				w = c
				break
			end
		end
		if w then
			local weight = CODEX_PLAN_WEIGHT[string.lower(str(r.loginMethod))] or 1
			table.insert(windows, { w = w, weight = weight })
			local recent = val(w.recent)
			if type(recent) == "table" then
				consumed += weight * (tonumber(val(recent.consumedPercent)) or 0)
				hours = tonumber(val(recent.hours)) or hours
			end
		end
	end
	if #windows < 2 then
		return nil
	end
	local used, elapsed, total, paced, soonest = 0, 0, 0, true, math.huge
	for _, e in windows do
		local u = math.clamp(tonumber(val(e.w.usedPercent)) or 0, 0, 100)
		local reset = epoch(e.w.resetsAt)
		local span_ = if val(e.w.startsAt) then windowSpan(e.w) else minutes * 60
		local remaining = if reset then reset - now else nil
		if not remaining or remaining > span_ then
			paced = false
		elseif remaining <= 0 then
			u = 0
		else
			elapsed += e.weight * (1 - remaining / span_) * 100
			soonest = math.min(soonest, remaining)
		end
		used += e.weight * u
		total += e.weight
	end
	return {
		windowMinutes = minutes,
		usedPercent = used / total,
		pacePct = if paced then elapsed / total else -1,
		total = total,
		remaining = (total * 100 - used) / 100,
		nextReset = if soonest < math.huge then now + soonest else nil,
		recent = if hours > 0 then { hours = hours, consumedPercent = consumed / total } else nil,
	}
end

-- One window: label, bar with pace tick and recent tail, and its reset line.
local function windowRow(key: string, label: string, w: any, now: number, stale: boolean, pool: any, credits: string): (Node, string?)
	local pct = math.clamp(if pool then pool.usedPercent else (tonumber(val(w.usedPercent)) or 0), 0, 100)
	local pace = -1
	if not stale then
		pace = if pool then pool.pacePct else pacePercent(w, now)
	end
	local settled = pace >= 3
	local lvl = if stale then nil else level(pct, if settled then pace else -1)
	local token = if lvl then LEVEL_TONE[lvl] else "muted"
	local props: { [string]: any } = {
		value = pct / 100,
		size = "md",
		grow = 1,
		role = if lvl == "orange" then "codexbar.bar-orange" else "codexbar.bar",
	}
	local percent = withp(ui.text({ ui.span(string.format("%d%%", math.floor(pct + 0.5)), if lvl and lvl ~= "ok" then LEVEL_TONE[lvl] else "mono") }),
		{ min = { w = "5ch" }, basis = "content", role = "codexbar.pct" })
	local recent = if stale or not setting("recentUsage") then nil else (if pool then pool.recent else val(w.recent))
	local tail = 0
	if type(recent) == "table" then
		tail = math.min(pct, math.max(0, tonumber(val(recent.consumedPercent)) or 0))
	end
	if tail > 0 then
		props.parts = {
			{ value = (pct - tail) / 100, token = token, label = "earlier" },
			{ value = tail / 100, token = token, label = "last " .. str(recent.hours) .. "h" },
		}
	else
		props.tone = token
	end
	if pace >= 0 then
		local tip
		local spanSecs = if pool then (tonumber(val(w.windowMinutes)) or 0) * 60 else windowSpan(w)
		if not settled then
			tip = "Window just started; the pace projection shows once it passes 3% elapsed"
		else
			local elapsedSecs = spanSecs * pace / 100
			tip = (if pool then "Average " else "") .. span(elapsedSecs) .. " elapsed of " .. span(spanSecs)
				.. " (" .. math.floor(pace + 0.5) .. "%)"
			if pct > pace then
				local toFull = if pct > 0 then elapsedSecs * (100 - pct) / pct else 0
				tip ..= ": over pace, projected " .. math.floor(math.min(999, pct * 100 / pace) + 0.5)
					.. "% at reset, hits 100% in ~" .. span(toFull)
			else
				tip ..= ": on pace, lasts until the reset"
			end
		end
		props.marks = { { at = pace / 100, tone = if settled and pct > pace then "error" else "neutral", title = tip } }
	end

	local meta: { any } = {}
	if not stale then
		local reset
		if pool then
			reset = string.format("%.1f of %d Plus allowances left", pool.remaining, pool.total)
			if pool.nextReset then
				reset ..= " · next reset " .. absoluteTime(pool.nextReset, now) .. " (" .. span(pool.nextReset - now) .. ")"
			end
		else
			reset = formatReset(w, now)
		end
		if reset ~= "" then
			table.insert(meta, ui.span(reset, "muted"))
		end
		if settled then
			table.insert(meta, ui.span(" · proj " .. math.floor(math.min(999, pct * 100 / pace) + 0.5) .. "%", "muted"))
		end
		if credits ~= "" then
			table.insert(meta, ui.span(" · ", "muted"))
			table.insert(meta, ui.span(credits, "accent"))
		end
		if type(recent) == "table" then
			local used = tonumber(val(recent.consumedPercent)) or 0
			local amount = if used > 0 and used < 1 then "<1" else tostring(math.floor(used + 0.5))
			table.insert(meta, ui.span("   " .. amount .. "% in last " .. str(recent.hours) .. "h", "dim"))
		end
	end
	local kids: { Node } = {
		ui.row({
			withp(ui.text({ ui.span(label, "muted") }), { min = { w = "11ch" }, basis = "content" }),
			ui.node("meter", props),
			percent,
		}),
	}
	if #meta > 0 then
		table.insert(kids, ui.row({ withp(ui.text(""), { min = { w = "11ch" }, basis = "content" }), ui.text(meta) }))
	end
	return withp(ui.lines(kids), { key = nodeKey(key), role = if pct < 1 then "codexbar.unused" else nil }), lvl
end

-- The windows a record shows: core windows always, unused extras hidden for one account.
local function recordRows(rec: any, now: number, keyPrefix: string, credits: boolean): ({ Node }, string?)
	local rows: { Node } = {}
	local worst: string? = nil
	local stale = val(rec.stale) == true
	local hideZero = (tonumber(val(rec.accountCount)) or 1) < 2
	local entries = {}
	for _, slot in { "primary", "secondary", "tertiary" } do
		local w = val(rec[slot])
		if type(w) == "table" and val(w.usedPercent) ~= nil then
			local used = tonumber(val(w.usedPercent)) or 0
			if not (hideZero and slot == "tertiary" and rec.id ~= "opencodego" and used < 0.01) then
				table.insert(entries, { w = w, slot = slot, extra = "" })
			end
		end
	end
	for _, x in val(rec.extraRateWindows) or {} do
		local w = val(x.window)
		if type(w) == "table" and val(w.usedPercent) ~= nil then
			if not (hideZero and (tonumber(val(w.usedPercent)) or 0) < 0.01) then
				table.insert(entries, { w = w, slot = str(x.id), extra = str(x.title) })
			end
		end
	end
	-- Saved resets ride on the weekly row, else the last one.
	local creditsAt = 0
	if credits and (rec.id == "codex" or rec.id == "claude") then
		creditsAt = #entries
		for i, e in entries do
			if tonumber(val(e.w.windowMinutes)) == 10080 and e.extra == "" then
				creditsAt = i
				break
			end
		end
	end
	for i, e in entries do
		local node, lvl = windowRow(
			keyPrefix .. e.slot,
			windowLabel(str(rec.id), e.slot, e.w, e.extra),
			e.w,
			now,
			stale,
			nil,
			if i == creditsAt then resetCredits(rec.resetCredits, now) else ""
		)
		table.insert(rows, node)
		worst = worse(worst, lvl)
	end
	return rows, worst
end

local function lastKnownLine(rec: any): Node
	local spans: { any } = { ui.span("Last known usage", "warning") }
	local at = epoch(val(rec.cachedAt) or val(rec.updatedAt))
	if at then
		table.insert(spans, ui.span(" · " .. absoluteTime(at, os.time()), "muted"))
	end
	local err = str(rec.refreshError)
	if err ~= "" then
		table.insert(spans, ui.span(" · refresh failed: " .. err, "muted"))
	end
	return ui.text(spans)
end

local function errorLine(rec: any): Node?
	local err = val(rec.error)
	if not err then
		return nil
	end
	local message = if type(err) == "table" then str(err.message) else str(err)
	return ui.text({ ui.span(if message ~= "" then message else "Fetch failed", "error") })
end

local function balanceOf(rec: any): string
	local text = str(rec.balanceText)
	if text ~= "" then
		return text
	end
	local usage = val(rec.openRouterUsage)
	if rec.id == "openrouter" and type(usage) == "table" and tonumber(val(usage.balance)) then
		return string.format("$%.2f left", tonumber(val(usage.balance)) :: number)
	end
	return ""
end

local FORECAST_TONE = { announced = "success", likely = "warning", unknown = "neutral" }

local function forecastState(f: any): string
	if type(f) ~= "table" or val(f.ok) ~= true then
		return ""
	end
	if type(val(f.signal)) == "table" then
		return "announced"
	end
	local p48 = tonumber(val(f.prob48h))
	if p48 and p48 >= 50 and epoch(f.expectedAt) then
		return "likely"
	end
	return "unknown"
end

local function forecastNodes(f: any, now: number): { Node }
	local state = forecastState(f)
	if state == "" then
		return {}
	end
	local details: { string } = {}
	if state == "announced" then
		local signal = f.signal
		local head = "reset"
		local deadline = epoch(signal.deadlineAt)
		if deadline then
			head = "by " .. absoluteTime(deadline, now) .. " (" .. leftUntil(deadline, now) .. ")"
		end
		local sp = tonumber(val(signal.percent))
		if sp then
			head ..= " · " .. math.floor(sp + 0.5) .. "% chance"
		end
		table.insert(details, head)
	elseif state == "likely" then
		local expected = epoch(f.expectedAt) :: number
		table.insert(details, "~ " .. absoluteTime(expected, now) .. " (" .. leftUntil(expected, now) .. ") · "
			.. math.floor((tonumber(val(f.prob48h)) or 0) + 0.5) .. "% chance within 48h")
	else
		local line = "next reset unknown"
		local wait = val(f.wait)
		local waited = if type(wait) == "table" then tonumber(val(wait.days)) else nil
		if waited and waited >= 0 then
			line ..= " · waited " .. math.floor(waited + 0.5) .. "d"
			local share = tonumber(val(wait.shorterShare))
			if share and share >= 0 and share <= 1 then
				line ..= ", longer than " .. math.floor(share * 100 + 0.5) .. "% of recent gaps"
			end
		end
		table.insert(details, line)
	end
	if val(f.stale) == true then
		details[1] ..= " (cached)"
	end
	local confidence = str(f.confidence)
	if state ~= "announced" and confidence ~= "" then
		table.insert(details, confidence .. " confidence")
	end
	local nodes: { Node } = {
		withp(ui.row({
			withp(ui.text({ ui.span("Forecast", "muted") }), { min = { w = "11ch" }, basis = "content" }),
			ui.badge(string.upper(state), FORECAST_TONE[state]),
			ui.text({ ui.span(table.concat(details, " · "), "muted") }),
		}), { key = "forecast" }),
	}
	local incident = val(f.incident)
	if type(incident) == "table" and val(incident.open) == true then
		local surfaces = {}
		for _, s in val(incident.surfaces) or {} do
			table.insert(surfaces, str(s))
		end
		table.insert(nodes, ui.text({ ui.span("Codex incident open"
			.. (if #surfaces > 0 then " (" .. table.concat(surfaces, ", ") .. ")" else "")
			.. ": compensation reset possible", "error") }))
	end
	local alert = str(f.alertSummary)
	local hint = val(f.hint)
	if alert == "" and type(hint) == "table" and type(val(hint.quote)) == "string" then
		local at = epoch(hint.at)
		alert = "Hint" .. (if at then " " .. monthDay(at) else "") .. ": “" .. oneLine(hint.quote) .. "”"
	end
	if alert ~= "" then
		table.insert(nodes, ui.text({ ui.span(alert, "dim") }))
	end
	return nodes
end

local function cardOf(id: string, head: { any }, body: { Node }, worst: string?, stale: boolean, failed: boolean): Node
	local card = ui.card(head, body)
	local tone = if failed then "error" elseif stale then "muted" elseif worst == "red" then "error"
		elseif worst == "orange" or worst == "yellow" then "warning" else nil
	return withp(card, { key = "p-" .. id, role = "codexbar." .. (PROVIDER_ICONS[id] or "gauge"), tone = tone })
end

local function rotationIndicator(snap: any, email: string): string
	local rotation = val(snap.codexRotation)
	local availability = if type(rotation) == "table" and type(val(rotation.accountAvailability)) == "table"
		then val(rotation.accountAvailability[email]) else nil
	return if availability == "open" then "🟢 " elseif availability == "blocked" then "🔒 " else ""
end

local function providerCards(snap: any, now: number): { Node }
	local byId: { [string]: { any } } = {}
	local enabled = setting("providers")
	for _, rec in val(snap.providers) or {} do
		local id = str(rec.id)
		if table.find(enabled, id) then
			byId[id] = byId[id] or {}
			table.insert(byId[id], rec)
		end
	end
	local cards: { Node } = {}
	for _, id in PROVIDER_ORDER do
		local recs = byId[id]
		if not recs then
			continue
		end
		local name = PROVIDER_NAMES[id] or id
		if id == "codex" and #recs > 1 then
			local body: { Node } = {}
			local worst: string? = nil
			local okRecs, anyStale = {}, false
			for _, r in recs do
				if val(r.ok) == true then
					table.insert(okRecs, r)
				end
				anyStale = anyStale or val(r.stale) == true
			end
			if not anyStale then
				for _, spec in { { 300, "composite5h", "all 5h" }, { 10080, "composite7d", "all 7d" } } do
					if setting(spec[2]) then
						local pool = composite(okRecs, spec[1], now)
						if pool then
							local node, lvl = windowRow("pool" .. spec[1], spec[3], pool, now, false, pool, "")
							table.insert(body, node)
							worst = worse(worst, lvl)
						end
					end
				end
			end
			for i, r in recs do
				local email = str(r.accountEmail)
				local head: { any } = {
					ui.span(rotationIndicator(snap, email) .. (if email ~= "" then email else "account " .. i), "strong"),
					ui.span("  " .. str(r.loginMethod), "muted"),
				}
				local rows: { Node } = {}
				local failure = errorLine(r)
				if failure then
					table.insert(rows, failure)
				else
					if val(r.stale) == true then
						table.insert(rows, lastKnownLine(r))
					end
					local recRows, lvl = recordRows(r, now, "a" .. i, true)
					for _, n in recRows do
						table.insert(rows, n)
					end
					worst = worse(worst, lvl)
				end
				table.insert(body, withp(ui.section(head, rows), { key = "acct-" .. nodeKey(email ~= "" and email or tostring(i)) }))
			end
			if setting("forecast") then
				for _, n in forecastNodes(val(snap.forecast), now) do
					table.insert(body, n)
				end
			end
			table.insert(cards, cardOf(id, { ui.span(name, "strong"), ui.span("  " .. #recs .. " accounts", "muted") }, body, worst, anyStale, false))
		else
			for i, rec in recs do
				local detail = if id == "claude" then str(rec.loginMethod) else str(rec.accountEmail)
				if detail == "" and id ~= "claude" then
					detail = str(rec.loginMethod)
				end
				-- Balance providers repeat their balance as the plan label.
				if string.find(detail, "^Balance") then
					detail = ""
				end
				local head: { any } = { ui.span(name, "strong") }
				if detail ~= "" then
					table.insert(head, ui.span("  " .. detail, "muted"))
				end
				local balance = balanceOf(rec)
				if balance ~= "" and val(rec.error) == nil then
					table.insert(head, ui.span("   " .. balance, "strong"))
				end
				local body: { Node } = {}
				local worst: string? = nil
				local failure = errorLine(rec)
				local stale = val(rec.stale) == true
				if failure then
					table.insert(body, failure)
				else
					if stale then
						table.insert(body, lastKnownLine(rec))
					end
					local rows, lvl = recordRows(rec, now, "r", true)
					for _, n in rows do
						table.insert(body, n)
					end
					worst = lvl
				end
				if id == "codex" and setting("forecast") then
					for _, n in forecastNodes(val(snap.forecast), now) do
						table.insert(body, n)
					end
				end
				table.insert(cards, cardOf(id .. (if i > 1 then tostring(i) else ""), head, body, worst, stale, failure ~= nil))
			end
		end
	end
	return cards
end

-- Status-line summary for the window half ------------------------------------

local function usageSummary(snap: any, now: number): { any }
	local out = {}
	local seen: { [string]: boolean } = {}
	local enabled = setting("providers")
	for _, rec in val(snap.providers) or {} do
		local id = str(rec.id)
		if not table.find(enabled, id) or seen[id] or val(rec.ok) ~= true or val(rec.error) ~= nil then
			continue
		end
		local parts, worst = {}, nil
		for _, slot in { "primary", "secondary" } do
			local w = val(rec[slot])
			if type(w) == "table" and val(w.usedPercent) ~= nil then
				local pct = math.clamp(tonumber(val(w.usedPercent)) or 0, 0, 100)
				table.insert(parts, math.floor(pct + 0.5) .. "%")
				if val(rec.stale) ~= true then
					local pace = pacePercent(w, now)
					worst = worse(worst, level(pct, if pace >= 3 then pace else -1))
				end
			end
		end
		if #parts == 0 then
			continue
		end
		seen[id] = true
		-- Claude and Codex always; the rest only when a window runs hot.
		if id == "claude" or id == "codex" or (worst and LEVEL_RANK[worst] >= 2) then
			table.insert(out, {
				id = id,
				icon = PROVIDER_ICONS[id] or "gauge",
				text = (if PROVIDER_ICONS[id] then "" else (PROVIDER_NAMES[id] or id) .. " ") .. table.concat(parts, " · "),
				tone = if val(rec.stale) == true then "muted" elseif worst then STATUS_TONE[worst] else nil,
			})
		end
	end
	table.sort(out, function(a, b)
		return (table.find(PROVIDER_ORDER, a.id) or 99) < (table.find(PROVIDER_ORDER, b.id) or 99)
	end)
	return out
end

local function recordKey(r: any): string
	return nodeKey(str(r.provider) .. "-" .. str(r.sessionId))
end

local function liveAgents(): { any }
	local list = {}
	local data = store.agents
	local includeNonTern, includeUntracked = setting("includeNonTern"), setting("includeUntracked")
	for _, a in (if type(data) == "table" then val(data.agents) else nil) or {} do
		if (includeNonTern or a.host == "tern") and (includeUntracked or a.state ~= "untracked") then
			table.insert(list, a)
		end
	end
	return list
end

local function historyRecords(): { any }
	local data = store.agents
	local includeNonTern = setting("includeNonTern")
	local live: { [string]: boolean } = {}
	for _, a in (if type(data) == "table" then val(data.agents) else nil) or {} do
		live[recordKey(a)] = true
	end
	local rows, exited = {}, {}
	for _, r in (if type(data) == "table" then val(data.history) else nil) or {} do
		if not live[recordKey(r)] and (includeNonTern or r.host == "tern") then
			table.insert(if r.closedBy == "reboot" then rows else exited, r)
		end
	end
	table.sort(exited, function(a, b)
		return (tonumber(val(a.lastSeenAt)) or 0) > (tonumber(val(b.lastSeenAt)) or 0)
	end)
	for i = 1, math.min(#exited, setting("historyLimit")) do
		table.insert(rows, exited[i])
	end
	return rows
end

local function publishSummary()
	local counts = { working = 0, blocked = 0, idle = 0, untracked = 0 }
	for _, a in liveAgents() do
		local s = str(a.state)
		counts[s] = (counts[s] or 0) + 1
	end
	local restore = 0
	for _, r in historyRecords() do
		if r.closedBy == "reboot" then
			restore += 1
		end
	end
	local summary = {
		usage = if store.usage then usageSummary(store.usage, os.time()) else tern.json.array(),
		counts = counts,
		restore = restore,
	}
	local sig = tern.json.encode(summary)
	if sig ~= store.summarySig then
		store.summarySig = sig
		tern.kv.set("summary", summary)
	end
end

local function publishOpen()
	local open = {}
	for pane, b in blocks do
		table.insert(open, { pane = pane, tab = b.tab, kind = "dashboard" })
	end
	for pane in settingsBlocks do
		table.insert(open, { pane = pane, kind = "settings" })
	end
	table.sort(open, function(a, b)
		return a.pane < b.pane
	end)
	local sig = tern.json.encode(open)
	if sig ~= store.openSig then
		store.openSig = sig
		tern.kv.set("open", tern.json.array(open))
	end
end

-- Pollers -------------------------------------------------------------------

local function scanAgents(force: boolean)
	if store.scanning then
		return
	end
	store.scanning = true
	renderAll()
	python("codexbar_agents.py", { "--once", "--history-limit", tostring(setting("historyLimit")) }, 30000, function(res)
		store.scanning = false
		if res.status ~= 0 then
			tern.log.warn("codexbar: agent sweep failed", firstLine(res.stderr))
		end
		store.agentsRaw = "" -- reread
	end)
end

local function readAgents(): boolean
	local ok, raw = pcall(tern.fs.read, AGENTS_FILE, 16 * 1024 * 1024)
	if not ok then
		raw = ""
	end
	if raw == store.agentsRaw then
		return false
	end
	store.agentsRaw = raw
	local okj, data = pcall(tern.json.decode, raw)
	store.agents = if okj and type(data) == "table" then data else nil
	return true
end

-- Newest measurement in a snapshot, in epoch seconds.
local function measuredAt(snap: any): number
	local newest = 0
	for _, rec in (if type(snap) == "table" then val(snap.providers) else nil) or {} do
		newest = math.max(newest, epoch(val(rec.cachedAt) or val(rec.updatedAt)) or 0)
	end
	return newest
end

-- The applet's poll interval as the plugin knows it (applet setting `refreshSeconds`).
local function refreshSeconds(): number
	return setting("refreshSeconds")
end

-- Usage is polled by the applet. The plugin fetches on its own only when no
-- process measured usage for two intervals, so it never adds requests while
-- the applet runs; every fetch is a Claude OAuth call, and when that one is
-- rate-limited the fetcher falls back to starting a Claude Code session.
-- `--min-interval` makes the fetcher's lock allow one fetch per interval
-- across every daemon and the applet; a refused one answers from the cache.
local function staleAfter(): number
	return 2 * refreshSeconds()
end

-- `--cache-only` marks every record last-known. A record measured recently
-- (normally by the applet), without a refresh error, is current: show it live.
local function adoptCache(data: any)
	local now = os.time()
	for _, rec in val(data.providers) or {} do
		local at = epoch(val(rec.cachedAt))
		if val(rec.stale) == true and str(rec.refreshError) == "" and at and now - at <= staleAfter() + 30 then
			rec.stale = false
		end
	end
	-- The cache keeps no forecast, rotation or CLI version; carry the last fetch's.
	local previous = store.usage
	if type(previous) == "table" then
		for _, k in { "forecast", "codexRotation", "cliVersion" } do
			if val(data[k]) == nil then
				data[k] = val(previous[k])
			end
		end
	end
	store.usage = data
end

local function applyUsage(res: ProcessResult)
	local okj, data = pcall(tern.json.decode, res.stdout)
	if res.status == 0 and okj and type(data) == "table" then
		store.usage = data
		local fatal = val(data.fatal)
		store.usageError = if type(fatal) == "table" then str(fatal.message) elseif fatal then str(fatal) else nil
	else
		store.usageError = if res.timed_out then "CodexBar AgentView did not answer in time" else firstLine(res.stderr)
	end
end

local function providerArgs(): { string }
	return { "--cli-path", cliPath(), "--providers", table.concat(setting("providers"), ",") }
end

local function fetchUsage(automatic: boolean?)
	if store.fetching or #setting("providers") == 0 then
		return
	end
	store.fetching = true
	local generation = store.usageGeneration
	renderAll()
	local args = providerArgs()
	if automatic then
		table.insert(args, "--min-interval")
		table.insert(args, tostring(refreshSeconds()))
	end
	if setting("forecast") then
		table.insert(args, "--forecast-url")
		table.insert(args, FORECAST_URL)
	end
	python("codexbar_fetch.py", args, 90000, function(res)
		store.fetching = false
		if generation ~= store.usageGeneration then
			renderAll()
			return
		end
		store.usageAt = os.time() -- also after a failure or a refusal, so retries wait a full interval
		local okj, data = pcall(tern.json.decode, res.stdout)
		if res.status == 0 and okj and type(data) == "table" and val(data.cacheOnly) == true then
			adoptCache(data)
		else
			applyUsage(res)
		end
		publishSummary()
		renderAll()
	end)
end

-- A settings reread is cache-only, including when the cache is stale.
local function readUsageCache(allowFetch: boolean?, reclassify: boolean?)
	if #setting("providers") == 0 then
		return
	end
	local generation = store.usageGeneration
	local previous = store.usage
	local args = providerArgs()
	table.insert(args, "--cache-only")
	python("codexbar_fetch.py", args, 30000, function(res)
		if generation ~= store.usageGeneration then
			return
		end
		local okj, data = pcall(tern.json.decode, res.stdout)
		if (not store.fetching or allowFetch == false) and res.status == 0 and okj and type(data) == "table"
			and ((allowFetch == false and not reclassify) or not store.usage or measuredAt(data) > measuredAt(store.usage)
				or (reclassify and store.usage == previous and measuredAt(data) == measuredAt(store.usage))) then
			adoptCache(data)
		end
		if allowFetch ~= false and os.time() - math.max(store.usageAt, measuredAt(store.usage)) >= staleAfter() then
			fetchUsage(true)
		end
		publishSummary()
		renderAll()
	end)
end

local function prune()
	local live: { [number]: boolean } = {}
	for _, p in tern.pane.list() do
		live[p.pane] = true
	end
	for pane in blocks do
		if not live[pane] then
			blocks[pane] = nil
		end
	end
	for pane in settingsBlocks do
		if not live[pane] then
			settingsBlocks[pane] = nil
		end
	end
end

local function applyGoto()
	local g = tern.kv.get("goto")
	if type(g) ~= "table" or g.seq == store.lastGoto then
		return
	end
	store.lastGoto = g.seq
	local target = tonumber(g.pane)
	if not target or not table.find(TABS, g.tab) then
		return
	end
	for pane, b in blocks do
		-- The window may only know the low 44 bits of the id.
		if pane % PANE_TAG == target % PANE_TAG then
			b.tab = g.tab
			b.sel = nil
			b.cx:render()
		end
	end
end

local ticks = 0
local function tick()
	ticks += 1
	local ok, err = pcall(function()
		applyGoto()
		if ticks % 4 == 0 then
			prune()
			publishOpen()
			if readAgents() then
				publishSummary()
				renderAll()
			end
			local updated = if type(store.agents) == "table" then tonumber(val(store.agents.updatedAt)) else nil
			if not updated or nowMs() - updated > AGENTS_STALE_MS then
				scanAgents(false)
			end
		end
		local now = os.time()
		if now - store.cacheReadAt >= CACHE_EVERY_S then
			store.cacheReadAt = now
			readUsageCache()
		end
		-- Ages and countdowns move once a minute.
		if ticks % 120 == 0 then
			renderAll()
		end
	end)
	if not ok then
		tern.log.error("codexbar:", err)
	end
	tern.timer(TICK_MS, tick)
end

-- Filtering (AgentsSection.qml) ---------------------------------------------

local function isWordByte(b: number): boolean
	return (b >= 48 and b <= 57) or (b >= 65 and b <= 90) or (b >= 97 and b <= 122) or b >= 128
end

local function wordStart(text: string, k: number): boolean
	if k == 1 then
		return true
	end
	local p, c = string.byte(text, k - 1), string.byte(text, k)
	if not isWordByte(p) then
		return true
	end
	-- camelCase boundary
	return c >= 65 and c <= 90 and p >= 97 and p <= 122
end

-- Matched byte positions of lowercase `q` in `text`: a substring, else a
-- subsequence whose every run of matched characters starts a word.
local function matchPositions(text: string, q: string): { number }?
	if text == "" or q == "" then
		return nil
	end
	local hay = string.lower(text)
	local at = string.find(hay, q, 1, true)
	if at then
		local run = {}
		for r = 0, #q - 1 do
			table.insert(run, at + r)
		end
		return run
	end
	local failed: { [number]: boolean } = {}
	local function walk(from: number, j: number): { number }?
		if j > #q then
			return {}
		end
		local key = from * 256 + j
		if failed[key] then
			return nil
		end
		local want = string.byte(q, j)
		for k = from, #hay do
			if string.byte(hay, k) == want and ((j > 1 and k == from) or wordStart(text, k)) then
				local rest = walk(k + 1, j + 1)
				if rest then
					table.insert(rest, 1, k)
					return rest
				end
			end
		end
		failed[key] = true
		return nil
	end
	return walk(1, 1)
end

local function modelName(model: any): string
	local m = string.match(str(model), "^%s*(.-)%s*$") or ""
	m = string.match(m, "([^/]*)$") or m
	return (string.gsub(m, "^claude%-", ""))
end

local function vendorToken(model: any): string
	local m = string.lower(str(model))
	m = string.match(m, "([^/]*)$") or m
	for _, p in { "^claude", "^opus", "^sonnet", "^haiku", "^fable" } do
		if string.find(m, p) then
			return "warning"
		end
	end
	if string.find(m, "^gpt") or string.find(m, "^codex") or string.find(m, "^o%d") then
		return "info"
	end
	return "muted"
end

local FIELDS = {
	{ name = "windowTitle", label = "title" },
	{ name = "lastPrompt", label = "prompt" },
	{ name = "cwd", label = "cwd" },
	{ name = "provider", label = "provider" },
	{ name = "model", label = "model" },
}

local function fieldText(r: any, name: string): string
	return oneLine(if name == "model" then modelName(r.model) else r[name])
end

local function excerpt(text: string, positions: { number }): string
	local first, last = positions[1], positions[#positions]
	local from = math.max(1, first - 12) -- the detail column is narrow: keep the match in view
	local to = math.min(#text, last + 80)
	-- Keep cuts on UTF-8 boundaries.
	while from > 1 and (string.byte(text, from) or 0) >= 128 and (string.byte(text, from) or 0) < 192 do
		from -= 1
	end
	while to < #text and (string.byte(text, to + 1) or 0) >= 128 and (string.byte(text, to + 1) or 0) < 192 do
		to += 1
	end
	return (if from > 1 then "… " else "") .. string.sub(text, from, to) .. (if to < #text then " …" else "")
end

local function recordMatches(r: any, q: string): boolean
	if q == "" then
		return true
	end
	for _, f in FIELDS do
		if matchPositions(fieldText(r, f.name), q) then
			return true
		end
	end
	for _, t in val(r.recent) or {} do
		if string.find(string.lower(oneLine(t.text)), q, 1, true) then
			return true
		end
	end
	return false
end

local function labelField(r: any): string
	if str(r.windowTitle) ~= "" then
		return "windowTitle"
	end
	if str(r.lastPrompt) ~= "" then
		return "lastPrompt"
	end
	return "provider"
end

-- The first match the row's label doesn't show, as a detail line.
local function filterSnippet(r: any, q: string): string?
	if q == "" then
		return nil
	end
	local shown = labelField(r)
	for _, f in FIELDS do
		if f.name ~= shown then
			local text = fieldText(r, f.name)
			local pos = matchPositions(text, q)
			if pos then
				return f.label .. ": " .. excerpt(text, pos)
			end
		end
	end
	for _, t in val(r.recent) or {} do
		local text = oneLine(t.text)
		local at = string.find(string.lower(text), q, 1, true)
		if at then
			local run = {}
			for i = 0, #q - 1 do
				table.insert(run, at + i)
			end
			return (if t.role == "user" then "> " else "· ") .. excerpt(text, run)
		end
	end
	return nil
end

-- Agents and History lists ---------------------------------------------------

local function turnNodes(turns: any): { Node }
	local out: { Node } = {}
	for i, t in val(turns) or {} do
		local tools = t.kind == "tools"
		local cls = if t.role == "user" then "cb-turn cb-user" elseif tools then "cb-turn cb-tools" else "cb-turn cb-asst"
		table.insert(out, ui.el("div", { class = cls, key = "t" .. i, text = (if tools then "⚙ " else "") .. oneLine(t.text) }))
	end
	return out
end

local function desktopSpan(d: any): Span
	local desk = str(d)
	if desk == "" then
		return ui.span("   ", "dim")
	end
	return ui.span(pad(desk, 3), "dim")
end

local STATE_TONE = { working = "success", blocked = "error", untracked = "info" }

local function stateGlyph(r: any, focused: boolean, now: number): Span
	local s = str(r.state)
	if s == "working" then
		return ui.span(if focused then "◉" else "●", "success")
	elseif s == "blocked" then
		return ui.span(if focused then "◈" else "◆", "error")
	elseif s == "untracked" then
		return ui.span(if focused then "◉" else "●", "info")
	end
	local changed = tonumber(val(r.stateChangedAt)) or 0
	return ui.span(if focused then "◎" else "○", if now - changed < 300000 then "success" else "muted")
end

local function focusedKey(list: { any }): string?
	local pane = tonumber(tern.kv.get("focus"))
	if not pane then
		return nil
	end
	local hit = nil
	for _, r in list do
		local own = tonumber(val(r.ternPane))
		if own and own % PANE_TAG == pane % PANE_TAG then
			if hit then
				return nil
			end
			hit = recordKey(r)
		end
	end
	return hit
end

type Row = { key: string, record: any, history: boolean }

-- Agents grouped by folder (alphabetical), newest state change first.
local function agentRows(q: string): { { folder: string, rows: { any } } }
	local groups: { [string]: { any } } = {}
	local order: { string } = {}
	for _, a in liveAgents() do
		if recordMatches(a, q) then
			local folder = cwdLabel(a.cwd)
			if folder == "" then
				folder = str(a.provider)
			end
			if not groups[folder] then
				groups[folder] = {}
				table.insert(order, folder)
			end
			table.insert(groups[folder], a)
		end
	end
	table.sort(order, function(a, b)
		return string.lower(a) < string.lower(b)
	end)
	local out = {}
	for _, folder in order do
		local rows = groups[folder]
		table.sort(rows, function(a, b)
			local at, bt = tonumber(val(a.stateChangedAt)) or 0, tonumber(val(b.stateChangedAt)) or 0
			if at ~= bt then
				return at > bt
			end
			return str(a.sessionId) < str(b.sessionId)
		end)
		table.insert(out, { folder = folder, rows = rows })
	end
	return out
end

-- Ended sessions that are not live again, matching `q`: restart rows by desktop, then exits newest first.
local function historyRows(q: string): ({ any }, { any })
	local reboot, exited = {}, {}
	for _, r in historyRecords() do
		if recordMatches(r, q) then
			table.insert(if r.closedBy == "reboot" then reboot else exited, r)
		end
	end
	local function newest(a, b)
		local at, bt = tonumber(val(a.lastSeenAt)) or 0, tonumber(val(b.lastSeenAt)) or 0
		if at ~= bt then
			return at > bt
		end
		return recordKey(a) < recordKey(b)
	end
	local function deskRank(r): number
		local d = str(r.desktop)
		return tonumber(d) or (if d == "all" then 1e9 else 2e9)
	end
	table.sort(reboot, function(a, b)
		local ad, bd = deskRank(a), deskRank(b)
		if ad ~= bd then
			return ad < bd
		end
		return newest(a, b)
	end)
	table.sort(exited, newest)
	return reboot, exited
end

local LAUNCH_HOSTS = { kitty = true, tern = true }

local function selectable(b: Block): { Row }
	local q = string.lower(b.filter)
	local out: { Row } = {}
	if b.tab == "agents" then
		for _, g in agentRows(q) do
			for _, r in g.rows do
				table.insert(out, { key = recordKey(r), record = r, history = false })
			end
		end
	elseif b.tab == "history" then
		local reboot, exited = historyRows(q)
		for _, list in { reboot, exited } do
			for _, r in list do
				table.insert(out, { key = recordKey(r), record = r, history = true })
			end
		end
	end
	return out
end

local function selected(b: Block): Row?
	local rows = selectable(b)
	for _, row in rows do
		if row.key == b.sel then
			return row
		end
	end
	return rows[1]
end

-- A capped list scrolls itself and keeps the selected row in view. The cap is
-- the whole window; codexbar.css then shrinks the list to the height left in
-- the pane (blocks report 80×24 cells whatever their size in Tern 0.4.5).
local LIST_MAX = 1

local function peekNode(b: Block, r: any, history: boolean): Node
	local kids: { Node } = {}
	local recap = str(r.recap)
	if recap ~= "" then
		table.insert(kids, ui.el("div", { class = "cb-recap", text = (if val(r.recapStale) == true then "Earlier: " else "") .. recap }))
	end
	if history then
		local where = { ui.span(shortPath(r.cwd), "path"), ui.span("  " .. str(r.host), "muted") }
		table.insert(kids, ui.text(where))
	end
	local buttons: { Node } = {}
	if history then
		if LAUNCH_HOSTS[str(r.host)] and str(r.resumeCommand) ~= "" then
			local busy = b.launched[recordKey(r)] and os.time() - b.launched[recordKey(r)] < 60
			table.insert(buttons, button(if busy then "Launching…" else "Launch", "launch", "accent"))
		end
		if str(r.resumeCommand) ~= "" then
			table.insert(buttons, button("Copy resume command", "copy_resume"))
		end
		table.insert(buttons, button("Dismiss", "dismiss"))
	else
		table.insert(buttons, button("Focus", "focus", "accent"))
		local ternInstalled = type(store.agents) == "table" and val(store.agents.ternInstalled) == true
		if ternInstalled and r.host == "kitty" and str(r.resumeCommand) ~= "" then
			local idle = r.state == "idle"
			table.insert(buttons, withp(button(if b.busy == recordKey(r) then "Teleporting…" else "Teleport to Tern", "teleport"),
				if idle then {} else { title = "Only idle sessions can move", class = "cb-btn off" }))
		end
		if str(r.resumeCommand) ~= "" then
			table.insert(buttons, button("Copy resume command", "copy_resume"))
		end
	end
	table.insert(buttons, button(if b.peek then "Hide conversation" else "Conversation", "peek"))
	table.insert(kids, ui.el("div", { class = "cb-actions", key = "actions" }, buttons))
	if b.peek then
		for _, n in turnNodes(r.recent) do
			table.insert(kids, n)
		end
	end
	return ui.el("div", { class = "cb-peek", key = "peek" }, kids)
end

local function agentItem(b: Block, r: any, focused: boolean, now: number, q: string): Node
	local title = fieldText(r, labelField(r))
	local idleFor = now - (tonumber(val(r.stateChangedAt)) or now)
	return ui.node("item", {
		key = recordKey(r),
		label = { stateGlyph(r, focused, now), " ", title },
		detail = filterSnippet(r, q) or "",
		value = {
			ui.span(pad(modelName(r.model), 15), vendorToken(r.model)),
			ui.span(pad(str(r.host), 7), "muted"),
			desktopSpan(r.desktop),
			ui.span(string.format("%4s", ageFrom(r.stateChangedAt, now)), ageToken(r.stateChangedAt, now, 900000, 86400000)),
		},
		icon = AGENT_ICONS[str(r.provider)] or "terminal",
		tone = STATE_TONE[str(r.state)],
		role = if r.state == "idle" and idleFor > 86400000 then "codexbar.old" else "codexbar.agent",
	})
end

local function historyItem(b: Block, r: any, now: number, q: string): Node
	local title = fieldText(r, labelField(r))
	local launchable = LAUNCH_HOSTS[str(r.host)] and str(r.resumeCommand) ~= ""
	return ui.node("item", {
		key = recordKey(r),
		label = title,
		detail = filterSnippet(r, q) or cwdLabel(r.cwd),
		value = {
			ui.span(pad(modelName(r.model), 14), vendorToken(r.model)),
			ui.span(string.format("%4s ", ageFrom(r.lastSeenAt, now)), ageToken(r.lastSeenAt, now, 3600000, 86400000)),
			desktopSpan(r.desktop),
		},
		icon = AGENT_ICONS[str(r.provider)] or "terminal",
		hint = if b.sel == recordKey(r) and launchable then { "enter" } else nil,
		role = "codexbar.history",
	})
end

local function groupHead(key: string, spans: { any }, buttons: { Node }?): Node
	local kids: { Node } = { ui.text(spans) }
	if buttons then
		for _, n in buttons do
			table.insert(kids, n)
		end
	end
	return withp(ui.row(kids), { key = key, role = "codexbar.group" })
end

local function listView(b: Block, children: { Node }, empty: string): Node
	local sel = selected(b)
	return ui.node("list", {
		key = "list",
		selected = if sel then "main.list." .. sel.key else nil,
		filter = b.filter,
		empty = { ui.span(empty, "muted") },
		max = LIST_MAX,
	}, children)
end

local function agentsView(b: Block, now: number): Node
	local q = string.lower(b.filter)
	local sel = selected(b)
	local list = liveAgents()
	local focus = focusedKey(list)
	local kids: { Node } = {}
	for _, g in agentRows(q) do
		table.insert(kids, groupHead("g-" .. nodeKey(g.folder), { ui.span(g.folder, "strong"), ui.span("  " .. #g.rows, "muted") }))
		for _, r in g.rows do
			local key = recordKey(r)
			table.insert(kids, agentItem(b, r, key == focus, now, q))
			if sel and sel.key == key then
				table.insert(kids, peekNode(b, r, false))
			end
		end
	end
	local empty = if b.filter ~= "" then "No session matches “" .. b.filter .. "”." else "No coding agents are running."
	return listView(b, kids, empty)
end

local function historyView(b: Block, now: number): Node
	local q = string.lower(b.filter)
	local sel = selected(b)
	local reboot, exited = historyRows(q)
	local kids: { Node } = {}
	local function add(r)
		local key = recordKey(r)
		table.insert(kids, historyItem(b, r, now, q))
		if sel and sel.key == key then
			table.insert(kids, peekNode(b, r, true))
		end
	end
	if #reboot > 0 then
		table.insert(kids, groupHead("h-reboot", { ui.span("Interrupted by restart", "strong"), ui.span("  " .. #reboot, "muted") }, {
			button("Restore all", "restore_all", "accent"),
			button("Dismiss all", "dismiss_all"),
		}))
		for _, r in reboot do
			add(r)
		end
	end
	if #exited > 0 then
		table.insert(kids, groupHead("h-exit", { ui.span("Recently closed", "strong"), ui.span("  " .. #exited, "muted") }))
		for _, r in exited do
			add(r)
		end
	end
	local empty = if b.filter ~= "" then "No closed session matches “" .. b.filter .. "”." else "No closed sessions yet."
	return listView(b, kids, empty)
end

local function key(text: string): Span
	return ui.span(text, "key")
end

local function dockView(b: Block, now: number): Node
	local status: { any } = {}
	local hints: { any } = { key("←"), key("→"), ui.span(" tabs   ", "dim") }
	if b.tab == "usage" then
		local snap = store.usage
		if store.fetching and #setting("providers") > 0 then
			table.insert(status, ui.span("Refreshing… ", "muted"))
		end
		local at = math.max(store.usageAt, measuredAt(snap))
		if at > 0 then
			table.insert(status, ui.span("Updated " .. absoluteTime(at, now), "muted"))
		end
		if type(snap) == "table" and str(snap.cliVersion) ~= "" then
			table.insert(status, ui.span(" · codexbar " .. str(snap.cliVersion), "muted"))
		end
		if store.usageError then
			table.insert(status, ui.span("  " .. store.usageError, "error"))
		end
		for _, n in { key("r"), ui.span(" refresh   ", "dim"), key("esc"), ui.span(" close", "dim") } do
			table.insert(hints, n)
		end
	else
		local c = { working = 0, blocked = 0, idle = 0, untracked = 0 }
		for _, a in liveAgents() do
			c[str(a.state)] = (c[str(a.state)] or 0) + 1
		end
		if b.tab == "agents" then
			table.insert(status, ui.span("● " .. c.working .. " working   ", "success"))
			table.insert(status, ui.span("◆ " .. c.blocked .. " blocked   ", if c.blocked > 0 then "error" else "muted"))
			table.insert(status, ui.span("○ " .. c.idle .. " idle", "muted"))
			if c.untracked > 0 then
				table.insert(status, ui.span("   ● " .. c.untracked .. " untracked", "info"))
			end
		else
			local reboot, exited = historyRows("")
			table.insert(status, ui.span(#reboot .. " to restore · " .. #exited .. " recently closed", "muted"))
		end
		if store.scanning then
			table.insert(status, ui.span("   scanning…", "dim"))
		end
		for _, n in {
			key("↑"), key("↓"), ui.span(" select   ", "dim"),
			key("⏎"), ui.span(if b.tab == "agents" then " focus   " else " launch   ", "dim"),
			key("space"), ui.span(" conversation   ", "dim"),
		} do
			table.insert(hints, n)
		end
		if b.tab == "history" then
			table.insert(hints, key("del"))
			table.insert(hints, ui.span(" dismiss   ", "dim"))
		end
		table.insert(hints, key("esc"))
		table.insert(hints, ui.span(if b.filter ~= "" then " clear" else " close", "dim"))
	end
	return ui.col({ ui.lines({ ui.text(status), ui.text(hints) }) })
end

-- Tern's `tabs` kind draws with no height in 0.4.5, so the strip is plain elements styled like it.
local function tabsNode(b: Block): Node
	local reboot, exited = historyRows("")
	local counts = {
		usage = "",
		agents = tostring(#liveAgents()),
		history = tostring(#reboot + #exited),
	}
	local tabs: { Node } = {}
	for _, id in TABS do
		local kids: { Node } = { ui.el("span", { text = TAB_NAMES[id] }) }
		if counts[id] ~= "" then
			table.insert(kids, ui.el("span", {
				class = "cb-count" .. (if id == "history" and #reboot > 0 then " warn" else ""),
				text = counts[id],
			}))
		end
		table.insert(tabs, ui.el("button", {
			key = id,
			class = "cb-tab" .. (if b.tab == id then " on" else ""),
			actions = { click = "tab=" .. id },
		}, kids))
	end
	table.insert(tabs, ui.el("button", {
		key = "settings", class = "cb-tab cb-settings", text = "⚙", title = "Settings",
		attrs = { ["aria-label"] = "Settings" }, actions = { click = "settings" },
	}))
	return ui.el("div", { key = "tabs", class = "cb-tabs", attrs = { role = "tablist" } }, tabs)
end

-- Actions -------------------------------------------------------------------

local function report(cx: BlockCx, what: string, res: ProcessResult)
	if res.status ~= 0 then
		local line = firstLine(res.stderr)
		cx:toast("error", "CodexBar AgentView: " .. what .. " failed", if line ~= "" then line else "exit " .. res.status)
	end
end

local function closeBlock(b: Block)
	if b.closed then
		return
	end
	b.closed = true
	blocks[b.cx.pane] = nil
	publishOpen()
	pcall(b.cx.exit, b.cx, 0) -- the window half may have closed the pane already
end

-- Switching to a session closes the dashboard, like picking from a popup.
local function focusRecord(b: Block, r: any)
	python("codexbar_focus.py", { "--tern", "codexbar-agentview://focus/" .. str(r.sessionId) }, 15000, function(res)
		report(b.cx, "focus", res)
		if res.status == 0 then
			closeBlock(b)
		end
	end)
end

local function launchRecord(b: Block, r: any)
	local key = recordKey(r)
	if not LAUNCH_HOSTS[str(r.host)] or str(r.resumeCommand) == "" then
		b.cx:toast("info", "CodexBar AgentView: " .. str(r.host) .. " sessions can't be relaunched", "Copy the resume command instead")
		return
	end
	if b.launched[key] and os.time() - b.launched[key] < 60 then
		return
	end
	b.launched[key] = os.time()
	python("codexbar_focus.py", { "--tern", "--launch", str(r.provider), str(r.sessionId) }, 60000, function(res)
		report(b.cx, "launch", res)
		if res.status ~= 0 then
			b.launched[key] = nil
		end
		scanAgents(true)
		b.cx:render()
	end)
end

local function pairsJson(records: { any }): string
	local pairs_ = {}
	for _, r in records do
		table.insert(pairs_, tern.json.array({ str(r.provider), str(r.sessionId) }))
	end
	return uriEncode(tern.json.encode(tern.json.array(pairs_)))
end

local function dismiss(b: Block, records: { any })
	if #records == 0 then
		return
	end
	python("codexbar_agents.py", { "--dismiss", pairsJson(records) }, 15000, function(res)
		report(b.cx, "dismiss", res)
		store.agentsRaw = ""
	end)
end

-- Relaunches the shown restart rows, skipping those launched within the last minute.
local function restoreAll(b: Block)
	local reboot = historyRows(string.lower(b.filter))
	local now = os.time()
	local targets = {}
	for _, r in reboot do
		local key = recordKey(r)
		local recent = b.launched[key] and now - b.launched[key] < 60
		if LAUNCH_HOSTS[str(r.host)] and str(r.resumeCommand) ~= "" and not recent then
			table.insert(targets, r)
			b.launched[key] = now
		end
	end
	if #targets == 0 then
		b.cx:toast("info", "CodexBar AgentView: nothing to restore", "Only kitty and Tern sessions relaunch")
		return
	end
	python("codexbar_focus.py", { "--tern", "--launch-all", pairsJson(targets) }, 300000, function(res)
		-- Keep the cooldown only for the pairs the helper reports as launched.
		local okj, launched = pcall(tern.json.decode, res.stdout)
		local done: { [string]: boolean } = {}
		for _, pair in (if okj and type(launched) == "table" then launched else {}) do
			done[nodeKey(str(pair[1]) .. "-" .. str(pair[2]))] = true
		end
		for _, r in targets do
			if not done[recordKey(r)] then
				b.launched[recordKey(r)] = nil
			end
		end
		report(b.cx, "restore", res)
		scanAgents(true)
		b.cx:render()
	end)
end

local function teleport(b: Block, r: any)
	if r.state ~= "idle" then
		b.cx:toast("info", "CodexBar AgentView: only idle sessions can move to Tern")
		return
	end
	b.busy = recordKey(r)
	python("codexbar_focus.py", { "--tern", "--teleport", str(r.provider), str(r.sessionId) }, 60000, function(res)
		b.busy = nil
		report(b.cx, "teleport", res)
		scanAgents(true)
		b.cx:render()
	end)
end

local function activate(b: Block)
	local row = selected(b)
	if not row then
		return
	end
	if row.history then
		launchRecord(b, row.record)
	else
		focusRecord(b, row.record)
	end
end

local function act(b: Block, name: string)
	local row = selected(b)
	if name == "refresh" then
		fetchUsage()
		scanAgents(true)
	elseif name == "settings" then
		tern.kv.set("settingsRequest", { pane = b.cx.pane, seq = tern.now() })
	elseif name == "restore_all" then
		restoreAll(b)
	elseif name == "dismiss_all" then
		dismiss(b, (historyRows(string.lower(b.filter))))
	elseif name == "peek" then
		b.peek = not b.peek
	elseif row then
		if name == "focus" then
			focusRecord(b, row.record)
		elseif name == "launch" then
			launchRecord(b, row.record)
		elseif name == "teleport" then
			teleport(b, row.record)
		elseif name == "copy_resume" then -- `copy` is Tern's own action: it copies the node's text
			b.cx:copy(str(row.record.resumeCommand))
			b.cx:toast("success", "Resume command copied")
		elseif name == "dismiss" then
			dismiss(b, { row.record })
		end
	end
end

local function move(b: Block, delta: number, absolute: number?)
	local rows = selectable(b)
	if #rows == 0 then
		return
	end
	-- Without a matching selection the first row is shown selected (`selected`), so moves start there.
	local i = 1
	for j, row in rows do
		if row.key == b.sel then
			i = j
		end
	end
	local target = absolute or (i + delta)
	b.sel = rows[math.clamp(target, 1, #rows)].key
end

-- The block -----------------------------------------------------------------

tern.block.define("dashboard", {
	init = function(cx: BlockCx, args: { string }, saved: any): Block
		-- A restart (plugin reload, daemon restart) keeps the tab shown; a new block opens on its launch argument.
		local tab = if type(saved) == "table" then saved.tab else args[1]
		if not table.find(TABS, tab) then
			tab = "agents"
		end
		local b: Block = { cx = cx, tab = tab, sel = nil, peek = false, filter = "", launched = {}, busy = nil }
		blocks[cx.pane] = b
		publishOpen()
		if not store.usage then
			readUsageCache()
		end
		readAgents()
		return b
	end,

	title = function(b: Block): string?
		return "CodexBar AgentView · " .. TAB_NAMES[b.tab]
	end,

	view = function(b: Block, cx: BlockCx): BlockView
		local now = os.time()
		local main: { Node } = { tabsNode(b) }
		if b.tab == "usage" then
			if #setting("providers") == 0 then
				table.insert(main, ui.text({ ui.span("Usage providers are disabled. Enable them in Settings.", "muted") }))
			elseif store.usage then
				for _, card in providerCards(store.usage, now) do
					table.insert(main, card)
				end
			elseif store.usageError then
				table.insert(main, ui.text({ ui.span(store.usageError, "error") }))
			else
				table.insert(main, ui.progress(nil, "Reading usage…"))
			end
		else
			table.insert(main, ui.node("input", {
				key = "filter",
				text = b.filter,
				cursor = #b.filter,
				placeholder = "Type to filter by title, prompt, folder, provider or model",
				prompt = { ui.span("⌕ ", "muted") },
			}))
			if not store.agents then
				table.insert(main, ui.progress(nil, "Scanning agents…"))
			elseif b.tab == "agents" then
				table.insert(main, agentsView(b, now * 1000))
			else
				table.insert(main, historyView(b, now * 1000))
			end
		end
		return { main = withp(ui.col(main), { role = "codexbar.main" }), dock = dockView(b, now) }
	end,

	key = function(b: Block, k: Key, cx: BlockCx): boolean?
		local name = k.name
		if k.ctrl or k.alt or k.meta then
			return false
		end
		if name == "left" or name == "right" then
			local i = table.find(TABS, b.tab) or 1
			i = math.clamp(i + (if name == "left" then -1 else 1), 1, #TABS)
			if TABS[i] == b.tab then
				return false
			end
			b.tab = TABS[i]
			b.sel = nil
			return true
		end
		if name == "escape" then
			if b.filter ~= "" then
				b.filter = ""
				b.sel = nil
				return true
			end
			closeBlock(b)
			return false
		end
		if b.tab == "usage" then
			if k.text == "r" or k.text == "R" then
				act(b, "refresh")
				return true
			end
			return false
		end
		if name == "up" then
			move(b, -1)
		elseif name == "down" then
			move(b, 1)
		elseif name == "page_up" then
			move(b, -10)
		elseif name == "page_down" then
			move(b, 10)
		elseif name == "home" then
			move(b, 0, 1)
		elseif name == "end" then
			move(b, 0, math.huge)
		elseif name == "space" then
			b.peek = not b.peek
		elseif name == "enter" then
			activate(b)
		elseif name == "delete" and b.tab == "history" then
			act(b, "dismiss")
		elseif name == "backspace" then
			if b.filter == "" then
				return false
			end
			local cut = utf8.offset(b.filter, -1) or #b.filter
			b.filter = string.sub(b.filter, 1, cut - 1)
			b.sel = nil
		elseif name == "paste" or (k.text and k.text ~= "" and string.byte(k.text) >= 32) then
			b.filter ..= oneLine(k.text)
			b.sel = nil
		else
			return false
		end
		return true
	end,

	event = function(b: Block, ev: UiEvent, cx: BlockCx)
		if ev.ev == "action" and ev.act == "tab" and table.find(TABS, ev.value) then
			b.tab = ev.value
			b.sel = nil
		elseif (ev.ev == "select" or ev.ev == "activate") and type(ev.item) == "string" then
			local prefix = "main.list."
			if string.sub(ev.item, 1, #prefix) == prefix then
				b.sel = string.sub(ev.item, #prefix + 1)
				if ev.ev == "activate" then
					activate(b)
				end
			end
		elseif ev.ev == "action" then
			act(b, tostring(ev.act))
		end
		publishOpen()
	end,

	save = function(b: Block): any
		return { tab = b.tab }
	end,
})

-- Native preferences --------------------------------------------------------

local SETTINGS_PAGES = {
	{ id = "usage", label = "Usage", icon = "gauge" },
	{ id = "providers", label = "Providers", icon = "cloud" },
	{ id = "agents", label = "Agents", icon = "users" },
	{ id = "general", label = "General", icon = "sliders" },
	{ id = "about", label = "About", icon = "info" },
}
local SETTINGS_LEADS = {
	usage = "Choose the details shown in the Usage tab. Changes save immediately.",
	providers = "Choose which providers appear in usage and the status line. Credentials stay in ~/.codexbar/config.json.",
	agents = "Choose which live agents and recently closed sessions appear.",
	general = "The plugin reads the applet's usage cache. It fetches only after two refresh intervals without a measurement, at most once per interval across Tern daemons and the applet. A live Claude fetch may start a probe session when rate-limited.",
	about = "CodexBar AgentView brings provider usage and coding-agent sessions to KDE Plasma and Tern. It uses the upstream CodexBar CLI for usage data.",
}
local SETTINGS_ROWS: { any } = {
	{ id = "forecast", page = "usage", section = "Details", label = "Codex reset forecast",
		hint = "Show the cached reset forecast. Enabling it does not fetch immediately." },
	{ id = "recentUsage", page = "usage", section = "Details", label = "Recent consumption",
		hint = "Show the last hour's 5h consumption and last day's weekly consumption." },
	{ id = "composite5h", page = "usage", section = "Codex accounts", label = "Combined 5h window",
		hint = "Show a plan-weighted total across current Codex accounts." },
	{ id = "composite7d", page = "usage", section = "Codex accounts", label = "Combined weekly window",
		hint = "Show a plan-weighted total across current Codex accounts." },
}
for _, provider in PROVIDER_ORDER do
	table.insert(SETTINGS_ROWS, {
		id = "provider." .. provider, provider = provider, page = "providers", section = "Usage providers",
		label = PROVIDER_NAMES[provider],
		hint = if provider == "typesafe" then "Balance only. Requires a manual cookie header in the credentials file."
			else "Use the shared provider cache and existing authentication.",
	})
end
for _, row in {
	{ id = "includeNonTern", page = "agents", section = "Session visibility", label = "Include non-Tern agents",
		hint = "Show agents from other hosts in Agents and History. Tested on Linux only; probably does not work on other platforms." },
	{ id = "includeUntracked", page = "agents", section = "Live sessions", label = "Include untracked agents",
		hint = "Show processes without a hook sentinel." },
	{ id = "historyLimit", page = "agents", section = "History", label = "Recently closed sessions",
		hint = "Keep 10 to 1000 same-boot exits. Sessions ended by a reboot are not limited.", kind = "number" },
	{ id = "refreshSeconds", page = "general", section = "Polling", label = "Usage refresh interval",
		hint = "60 to 3600 seconds. Match the applet's interval; fallback waits twice this interval.", kind = "number", unit = "s" },
	{ id = "cliPath", page = "general", section = "CLI", label = "CodexBar executable",
		hint = "Empty selects the installed CLI automatically. Enter saves, Escape cancels; Ctrl+A clears the draft.", kind = "text" },
	{ id = "about.kde", page = "about", section = "Projects", label = "CodexBar AgentView",
		hint = "https://github.com/materemias/codexbar-agentview", kind = "action" },
	{ id = "about.upstream", page = "about", section = "Projects", label = "CodexBar",
		hint = "https://github.com/steipete/CodexBar", kind = "action" },
} do
	table.insert(SETTINGS_ROWS, row)
end
local SETTINGS_BY_ID: { [string]: any } = {}
for _, row in SETTINGS_ROWS do
	SETTINGS_BY_ID[row.id] = row
end

local function rowValue(row: any, defaults: boolean?): any
	if row.kind == "action" then
		return nil
	end
	if row.provider then
		return table.find(if defaults then DEFAULTS.providers else setting("providers"), row.provider) ~= nil
	end
	return if defaults then DEFAULTS[row.id] else setting(row.id)
end

local function settingsSections(b: SettingsBlock): ({ any }, { any })
	local sections, rows = {}, {}
	local query = string.lower(b.query)
	for _, row in SETTINGS_ROWS do
		local matches = row.page == b.page
		if string.find(query, "%S") then
			matches = true
			local text = string.lower(row.label .. " " .. row.hint .. " " .. row.section .. " " .. row.page .. " " .. row.id)
			for word in string.gmatch(query, "%S+") do
				if not string.find(text, word, 1, true) then
					matches = false
					break
				end
			end
		end
		if not matches then
			continue
		end
		local value, default = rowValue(row), rowValue(row, true)
		local control: any
		if row.kind == "action" then
			control = { k = "action", act = "open_github", label = "Open GitHub" }
		elseif row.kind == "number" then
			local limits = NUMBER_LIMITS[row.id]
			control = { k = "number", value = value, min = limits[1], max = limits[2], step = 1, unit = row.unit }
		elseif row.kind == "text" then
			control = { k = "text", value = value, mono = true, placeholder = "Automatic" }
		else
			control = { k = "switch", on = value }
		end
		local section = sections[#sections]
		if not section or section.page ~= row.page or section.title ~= row.section then
			section = { id = row.page .. "." .. nodeKey(row.section), title = row.section, page = row.page, rows = {} }
			table.insert(sections, section)
		end
		table.insert(section.rows, {
			id = row.id, label = row.label, hint = row.hint, changed = value ~= default,
			defaultLabel = if row.kind == "action" then nil
				elseif type(default) == "boolean" then (if default then "On" else "Off")
				elseif default == "" then "Automatic" else tostring(default),
			control = control,
		})
		table.insert(rows, row)
	end
	return sections, rows
end

local function saveSetting(b: SettingsBlock, row: any, value: any): boolean
	if row.kind == "action" then
		return false
	end
	local name = if row.provider then "providers" else row.id
	-- Native prefs pointer Reset decodes JSON null as nil; keyboard Reset uses the sentinel.
	local reset = value == nil or value == tern.json.null
	if row.provider then
		if reset then
			value = rowValue(row, true)
		end
		if type(value) ~= "boolean" then
			b.cx:toast("error", "CodexBar AgentView: invalid provider setting")
			return false
		end
		local providers = {}
		for _, provider in setting("providers") do
			table.insert(providers, provider)
		end
		local i = table.find(providers, row.provider)
		if value and not i then
			table.insert(providers, row.provider)
		elseif not value and i then
			table.remove(providers, i)
		end
		value = providers
	elseif reset then
		value = DEFAULTS[name]
	end
	local parsed = parseSetting(name, value)
	if parsed == nil then
		b.cx:toast("error", "CodexBar AgentView: invalid setting", row.label)
		return false
	end
	local old = setting(name)
	local raw = tern.kv.get("settings")
	local settings = if type(raw) == "table" then table.clone(raw) else {}
	settings[name] = if reset and not row.provider then nil else parsed
	tern.kv.set("settings", settings)
	if tern.json.encode(old) ~= tern.json.encode(parsed) then
		if name == "providers" or name == "cliPath" then
			-- Pending helpers belong to the selection they started with.
			store.usageGeneration += 1
			store.usageError = nil
			if name == "cliPath" then
				store.usage = nil
			end
			store.cacheReadAt = os.time()
			readUsageCache(false)
		elseif name == "refreshSeconds" then
			store.cacheReadAt = os.time()
			-- Reuse equally recent measurements with the new stale threshold,
			-- but don't replace a fresh result delivered while the cache was read.
			readUsageCache(false, true)
		end
		publishSummary()
	end
	renderAll()
	return true
end

-- Cursor state counts Unicode codepoints; native prefs carets count UTF-16 units.
local function textByte(text: string, cursor: number): number
	return utf8.offset(text, cursor + 1) or (#text + 1)
end

local function prefsCursor(text: string, cursor: number): number
	local units = 0
	for _, code in utf8.codes(string.sub(text, 1, textByte(text, cursor) - 1)) do
		units += if code > 0xFFFF then 2 else 1
	end
	return units
end

local function editSettingsText(text: string, cursor: number, k: Key): (string, number, boolean)
	local length = utf8.len(text) :: number
	if k.ctrl and k.name == "a" then
		return "", 0, true
	elseif k.ctrl or k.alt or k.meta then
		return text, cursor, false
	elseif k.name == "left" then
		return text, math.max(0, cursor - 1), true
	elseif k.name == "right" then
		return text, math.min(length, cursor + 1), true
	elseif k.name == "home" then
		return text, 0, true
	elseif k.name == "end" then
		return text, length, true
	elseif k.name == "backspace" then
		if cursor > 0 then
			return string.sub(text, 1, textByte(text, cursor - 1) - 1) .. string.sub(text, textByte(text, cursor)), cursor - 1, true
		end
		return text, cursor, true
	elseif k.name == "delete" then
		return string.sub(text, 1, textByte(text, cursor) - 1) .. string.sub(text, textByte(text, math.min(length, cursor + 1))), cursor, true
	elseif k.name == "paste" or k.name == "space" or (k.text and k.text ~= "" and string.byte(k.text) >= 32) then
		local added = if k.name == "space" then " " else oneLine(k.text)
		local count = utf8.len(added)
		if count == nil or string.find(added, "%z") then
			return text, cursor, false
		end
		local at = textByte(text, cursor)
		return string.sub(text, 1, at - 1) .. added .. string.sub(text, at), cursor + count, true
	end
	return text, cursor, false
end

local function focusSettingsRow(b: SettingsBlock, delta: number)
	local _, rows = settingsSections(b)
	local i = 0
	if not b.searching then
		for j, row in rows do
			if row.id == b.focus then
				i = j
				break
			end
		end
	end
	i = (i + delta) % (#rows + 1)
	b.searching = i == 0
	b.focus = if i > 0 then rows[i].id else nil
	b.editing = nil
end

local function activateSetting(b: SettingsBlock, row: any)
	b.focus = row.id
	b.searching = false
	if row.kind == "action" then
		local ok, err = pcall(tern.process.run, { "xdg-open", row.hint }, { timeout_ms = 10000 }, function(res: ProcessResult)
			report(b.cx, "open GitHub", res)
		end)
		if not ok then
			report(b.cx, "open GitHub", { status = -1, stdout = "", stderr = tostring(err), timed_out = false })
		end
	elseif row.kind == "text" then
		local value = rowValue(row)
		b.editing = { row = row.id, draft = value, cursor = utf8.len(value) :: number }
	elseif row.kind ~= "number" then
		saveSetting(b, row, not rowValue(row))
	end
end

local function closeSettings(b: SettingsBlock)
	settingsBlocks[b.cx.pane] = nil
	publishOpen()
	pcall(b.cx.exit, b.cx, 0)
end

tern.block.define("settings", {
	init = function(cx: BlockCx, _args: { string }, saved: any): SettingsBlock
		local page = if type(saved) == "table" then saved.page else nil
		if not SETTINGS_LEADS[page] then
			page = "usage"
		end
		local b: SettingsBlock = { cx = cx, page = page, query = "", cursor = 0, searching = true, focus = nil, editing = nil }
		settingsBlocks[cx.pane] = b
		publishOpen()
		return b
	end,

	title = function(_b: SettingsBlock): string?
		return "CodexBar AgentView · Settings"
	end,

	view = function(b: SettingsBlock, _cx: BlockCx): BlockView
		local sections = settingsSections(b)
		local pages = {}
		for _, page in SETTINGS_PAGES do
			local changed = 0
			for _, row in SETTINGS_ROWS do
				if row.page == page.id and rowValue(row) ~= rowValue(row, true) then
					changed += 1
				end
			end
			table.insert(pages, { id = page.id, label = page.label, icon = page.icon, changed = changed })
		end
		local editing = b.editing
		return { main = ui.col({ ui.node("prefs", {
			key = "settings", title = "CodexBar AgentView Settings", pages = pages, page = b.page,
			-- Native controls consume their clicks; search and whitespace reach this action.
			actions = { click = "search_focus" },
			lead = SETTINGS_LEADS[b.page],
			sections = sections, query = b.query, focus = b.focus,
			cursor = if b.searching then prefsCursor(b.query, b.cursor) else nil,
			editing = if editing then { row = editing.row, draft = editing.draft, cursor = prefsCursor(editing.draft, editing.cursor) } else nil,
		}) }) }
	end,

	key = function(b: SettingsBlock, k: Key, _cx: BlockCx): boolean?
		if k.name == "escape" then
			if b.editing then
				b.editing = nil
			elseif b.query ~= "" then
				b.query, b.cursor, b.focus, b.searching = "", 0, nil, true
			else
				closeSettings(b)
			end
			return true
		elseif k.ctrl and k.name == "f" then
			b.editing, b.focus, b.searching = nil, nil, true
			return true
		elseif k.name == "tab" or k.name == "up" or k.name == "down" then
			focusSettingsRow(b, if k.name == "up" or (k.name == "tab" and k.shift) then -1 else 1)
			return true
		end
		if b.editing then
			local edit = b.editing
			if k.name == "enter" then
				if saveSetting(b, SETTINGS_BY_ID[edit.row], edit.draft) then
					b.editing = nil
				end
				return true
			end
			local handled
			edit.draft, edit.cursor, handled = editSettingsText(edit.draft, edit.cursor, k)
			return handled
		end
		if k.ctrl or k.alt or k.meta then
			if not (b.searching and k.ctrl and k.name == "a") then
				return false
			end
		end
		local row = b.focus and SETTINGS_BY_ID[b.focus]
		if not b.searching and row then
			if k.name == "space" or k.name == "enter" then
				activateSetting(b, row)
				return true
			elseif k.name == "delete" then
				saveSetting(b, row, tern.json.null)
				return true
			elseif row.kind == "number" and (k.text == "+" or k.text == "-" or k.name == "+" or k.name == "-") then
				local limits = NUMBER_LIMITS[row.id]
				saveSetting(b, row, math.clamp(rowValue(row) + (if k.text == "-" or k.name == "-" then -1 else 1), limits[1], limits[2]))
				return true
			end
		end
		-- Typing off a control always starts/continues search.
		local text, cursor, handled = editSettingsText(b.query, b.cursor, k)
		if handled then
			b.query, b.cursor, b.focus, b.searching = text, cursor, nil, true
		end
		return handled
	end,

	event = function(b: SettingsBlock, ev: UiEvent, _cx: BlockCx)
		local row = if type(ev.item) == "string" then SETTINGS_BY_ID[ev.item] else nil
		if ev.ev == "change" and row then
			b.focus, b.searching, b.editing = row.id, false, nil
			saveSetting(b, row, ev.value)
		elseif ev.ev == "activate" and row then
			activateSetting(b, row)
		elseif ev.ev == "select" and row then
			b.focus, b.searching, b.editing = row.id, false, nil
		elseif ev.ev == "action" and ev.act == "open_github" and type(ev.value) == "string" then
			local actionRow = SETTINGS_BY_ID[ev.value]
			if actionRow and actionRow.kind == "action" then
				activateSetting(b, actionRow)
			end
		elseif ev.ev == "action" and ev.act == "page" and type(ev.value) == "string" and SETTINGS_LEADS[ev.value] then
			b.page, b.query, b.cursor, b.focus, b.editing, b.searching = ev.value, "", 0, nil, nil, true
		elseif (ev.ev == "action" and ev.act == "search_focus") or ev.ev == "focus" then
			b.focus, b.editing, b.searching = nil, nil, true
		elseif ev.ev == "action" and ev.act == "close" and (ev.value == nil or ev.value == tern.json.null) then
			closeSettings(b)
		end
	end,

	save = function(b: SettingsBlock): any
		return { page = b.page }
	end,
})

local initialGoto = tern.kv.get("goto")
store.lastGoto = if type(initialGoto) == "table" then initialGoto.seq else nil
tern.timer(0, tick)
