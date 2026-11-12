-- Host half of the CodexBar AgentView plugin: one poller per daemon, dashboard and settings.
--
-- Data comes from the KDE applet's helpers next to this folder
-- (../contents/scripts): codexbar_fetch.py is the provider normalization path
-- and codexbar_agents.py writes agents.json under $XDG_STATE_HOME/codexbar-agentview
-- (default ~/.local/state/codexbar-agentview). While the applet runs,
-- its data is fresh and reused; the plugin only runs a helper when the data it
-- would read is older than the applet's own cadence.
--
-- codexbar_waiting.py owns the sessions set aside. This half calls its `tick`,
-- shows its view on the Waiting tab and publishes what the window half needs:
-- `waitingView`, the outbox `waitingActions`, `paneSessions`, the set-aside
-- items `waitingMenu`; the window half answers with `waitingRequest`, which
-- `codexbar-agentview://request` wakes it for, and `waitingPoke`.

local ui = tern.ui

local HOME = tern.getenv("HOME") or ""
local MACOS = tern.runtime.os == "macos"

local function pythonPath(): string
	if not MACOS then
		return "python3"
	end
	-- Honour a shell's Python, but Apple's /usr/bin/python3 can be an older
	-- developer-tools shim. GUI launches should use Homebrew before that.
	for dir in string.gmatch(tern.getenv("PATH") or "", "[^:]+") do
		if dir ~= "/usr/bin" and dir ~= "/usr/bin/" then
			local p = dir .. "/python3"
			if tern.fs.exists(p) then
				return p
			end
		end
	end
	for _, p in { "/opt/homebrew/bin/python3", "/usr/local/bin/python3" } do
		if tern.fs.exists(p) then
			return p
		end
	end
	return "python3"
end

local PYTHON = pythonPath()
local SCRIPTS = tern.plugin.dir .. "/../contents/scripts"
local XDG_STATE = tern.getenv("XDG_STATE_HOME") or ""
local STATE_DIR = (if string.sub(XDG_STATE, 1, 1) == "/" then XDG_STATE else HOME .. "/.local/state") .. "/codexbar-agentview"
local AGENTS_FILE = STATE_DIR .. "/agents.json"
local WAITING_FILE = STATE_DIR .. "/waiting.json"
local WAITING_VIEW_FILE = STATE_DIR .. "/waiting_view.json"
local WAITING_CLI = "codexbar_waiting.py"
-- `tick` runs `command` waits (60 s each, six at a time) and GitHub polls; a
-- killed tick loses the checks it already claimed.
local WAITING_TIMEOUT_MS = 600000
local WAITING_RETRY_S = 60

-- The window half runs the helpers too, with the same interpreter.
tern.kv.set("python", PYTHON)

-- One-shot: carry settings over from the plugin's former id `codexbar`.
if tern.kv.get("settings") == nil then
	local ok, raw = pcall(tern.fs.read, tern.plugin.data .. "/../codexbar/kv.json", 1024 * 1024)
	local okj, old = pcall(tern.json.decode, if ok then raw else "")
	if okj and type(old) == "table" and type(old.settings) == "table" then
		tern.kv.set("settings", old.settings)
	end
end
local FORECAST_URL = "https://codex-reset.com/api/forecast"

local TICK_MS = 500
local AGENTS_STALE_MS = 15000 -- the applet sweeps every 5 s
local CACHE_EVERY_S = 10
-- Each cache read runs a Python helper, which costs the daemon about 0.1 s of
-- CPU, so a read whose input files are unchanged is skipped for up to this long.
local CACHE_FORCE_S = 60

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
local AGENT_NAMES = { claude = "Claude Code", codex = "Codex", omp = "omp", pi = "pi" }
local CODEX_PLAN_WEIGHT = { plus = 1, pro = 20 }
local TABS = { "usage", "agents", "waiting", "history" }
local TAB_NAMES = { usage = "Usage", agents = "Agents", waiting = "Waiting", history = "History" }

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
	claudeComposite5h = true,
	claudeComposite7d = true,
	recentUsage = true,
	includeUntracked = true,
	includeNonTern = false,
	hideOld = false,
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
		if MACOS then "/opt/homebrew/bin/codexbar" else "/usr/local/bin/codexbar",
		if MACOS then "/usr/local/bin/codexbar" else "/opt/homebrew/bin/codexbar",
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

-- Seconds since the epoch of an ISO 8601 time; without an offset it counts as UTC.
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
	local sign, offsetH, offsetM = string.match(iso, "([+-])(%d%d):?(%d%d)$")
	local offset = if sign then (tonumber(offsetH) :: number) * 3600 + (tonumber(offsetM) :: number) * 60 else 0
	return (era * 146097 + doe - 719468) * 86400
		+ (tonumber(hh) :: number) * 3600
		+ (tonumber(mm) :: number) * 60
		+ (tonumber(ss) :: number)
		- (if sign == "-" then -offset else offset)
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

-- A row-sized native button; `props` adds variant, keys, busy, disabled or a tooltip.
local function button(text: string, act: string, props: { [string]: any }?): Node
	return withp(ui.button(text, { key = act, size = "sm", actions = { click = act } }), props or {})
end

-- Store: one per daemon, shared by every open dashboard -----------------------

local store = {
	usage = nil :: any,
	usageAt = 0,
	fetching = false,
	cacheReadAt = 0,
	cacheSpawnAt = 0,
	cacheSeq = 0,
	cacheInputs = "",
	cachePaths = {} :: { string },
	usageGeneration = 0,
	usageError = nil :: string?,
	agents = nil :: any,
	agentsRaw = "",
	scanning = false,
	summarySig = "",
	openSig = "",
	lastGoto = nil :: any,
	waiting = nil :: any, -- the CLI's last view
	waitingUncheckedSince = nil :: number?, -- os.time() a view no tick had checked yet first came, while none has
	waitingError = nil :: string?, -- the last tick's failure, cleared by a tick that succeeds
	waitingActions = nil :: any, -- the outbox as last published
	-- os.clock() below is Luau's: seconds on the system's monotonic clock
	-- (CLOCK_MONOTONIC on Linux, mach_absolute_time on macOS), not CPU time. The
	-- wall clock can't move it and, unlike tern.now(), which starts at 0 in each
	-- VM, every VM on the machine reads the same one, so the window half's leader
	-- lease uses it too. It stands still in sleep and starts over at a reboot.
	waitingAcking = {} :: { [string]: number }, -- action id -> no ack of it before then, os.clock() (math.huge: sent)
	waitingRaw = "", -- waiting.json as our last tick left it
	waitingSig = "",
	waitingActionsSig = "",
	waitingAgents = "",
	waitingTicking = false,
	waitingAgain = false,
	waitingRecheck = false, -- the next tick runs for another writer's change
	-- When the next timed tick is due, by the wall clock and by os.clock(): the first
	-- to come runs it, so neither a sleep nor a wall clock set back holds it up.
	waitingAt = 0,
	waitingAtMono = 0,
	waitingSkipped = nil :: string?, -- another Tern daemon ticks this state: why, as the CLI said
	daemonId = nil :: string?, -- `tick --daemon`: which daemon process this is (set at load, at the end)
	daemonKnown = false, -- whether that answer came, with an id or without one
	paneGoneAt = {} :: { [number]: number }, -- watched job pane -> os.clock() when the pane list lost it (math.huge: told)
	panesListed = false, -- whether this daemon's pane list has named a pane: only then is an empty one evidence
	jobEventAt = {} :: { [number]: number }, -- pane -> os.clock() when its last job event went to the CLI
	waitingMemo = nil :: any, -- waitingGroups' last answer, for one view and filter
	lastPoke = nil :: any,
	menuRetryAt = nil :: number?, -- os.clock() when a failed `menu` load runs again
	loadMenu = nil :: (() -> ())?,
}

type Block = {
	cx: BlockCx,
	tab: string,
	sel: string?,
	peek: boolean,
	filter: string,
	launched: { [string]: number }, -- row key -> os.clock() of its launch: the wall clock may be set back
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
	local argv = { PYTHON, SCRIPTS .. "/" .. script }
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

-- Pooled windows across one provider's accounts, in base-plan-sized allowances
-- (main.qml compositeStats): Codex in Plus, Claude in Pro via the fetch's planWeight.
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
			local weight = if str(r.id) == "codex"
				then CODEX_PLAN_WEIGHT[string.lower(str(r.loginMethod))] or 1
				else math.max(1, tonumber(val(r.planWeight)) or 1)
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
		unit = if records[1] and str(records[1].id) == "claude" then "Pro" else "Plus",
		usedPercent = used / total,
		pacePct = if paced then elapsed / total else -1,
		total = total,
		remaining = (total * 100 - used) / 100,
		nextReset = if soonest < math.huge then now + soonest else nil,
		recent = if hours > 0 then { hours = hours, consumedPercent = consumed / total } else nil,
		accounts = windows,
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
	if stale and pool then
		-- A last-known pool keeps its allowance count, not its reset or pace.
		table.insert(meta, ui.span(string.format("%.1f of %d %s allowances left", pool.remaining, pool.total, pool.unit), "muted"))
	elseif not stale then
		local reset
		if pool then
			reset = string.format("%.1f of %d %s allowances left", pool.remaining, pool.total, pool.unit)
			if pool.nextReset then
				reset ..= " · next reset " .. absoluteTime(pool.nextReset, now) .. " (" .. span(pool.nextReset - now) .. ")"
			end
		else
			-- Claude reports an unused window as 0% without a reset: the next
			-- window opens on the first message.
			local minutes = tonumber(val(w.windowMinutes)) or 0
			if tonumber(val(w.usedPercent)) == 0 and minutes > 0 and not epoch(w.resetsAt) and str(w.resetDescription) == "" then
				local length = if minutes == 300 then "5h" elseif minutes == 1440 then "1d"
					elseif minutes == 10080 then "7d" else span(minutes * 60)
				reset = "not started · resets " .. length .. " after first use"
			else
				reset = formatReset(w, now)
			end
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

local function rotationIndicator(snap: any, id: string, email: string): string
	local rotation = val(if id == "claude" then snap.claudeRotation elseif id == "codex" then snap.codexRotation else nil)
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
		-- Codex, and Claude with several accounts, render one card with a section per account.
		if (id == "codex" or id == "claude") and #recs > 1 then
			local body: { Node } = {}
			local worst: string? = nil
			local okRecs, anyStale = {}, false
			for _, r in recs do
				if val(r.ok) == true then
					table.insert(okRecs, r)
				end
				anyStale = anyStale or val(r.stale) == true
			end
			-- Any account on last-known usage makes the pool last-known too.
			local keys = if id == "claude" then { "claudeComposite5h", "claudeComposite7d" } else { "composite5h", "composite7d" }
			for _, spec in { { 300, keys[1], "all 5h" }, { 10080, keys[2], "all 7d" } } do
				if setting(spec[2]) then
					local pool = composite(okRecs, spec[1], now)
					if pool then
						local node, lvl = windowRow("pool" .. spec[1], spec[3], pool, now, anyStale, pool, "")
						table.insert(body, node)
						worst = worse(worst, lvl)
					end
				end
			end
			for i, r in recs do
				local email = str(r.accountEmail)
				local head: { any } = {
					ui.span(rotationIndicator(snap, id, email) .. (if email ~= "" then email else "account " .. i), "strong"),
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
			if id == "codex" and setting("forecast") then
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

-- Widget bar and status summary for the window half ---------------------------

-- A do block: the main chunk is at Luau's 200-local limit.
local usageWidgets: (snap: any, now: number) -> { [string]: any }
do
	-- The 5h and weekly windows the Claude and Codex widgets pool across accounts.
	local POOLED_WINDOWS = { { 300, "5h" }, { 10080, "7d" } }

	local function windowOf(rec: any, minutes: number): any
		for _, slot in { "primary", "secondary" } do
			local w = val(rec[slot])
			if type(w) == "table" and tonumber(val(w.windowMinutes)) == minutes and val(w.usedPercent) ~= nil then
				return w
			end
		end
		return nil
	end

	local function roundPct(pct: number): string
		return math.floor(math.clamp(pct, 0, 100) + 0.5) .. "%"
	end

	-- Tern keeps a graph widget's newest 120 samples.
	local SAMPLES = 120

	-- The window's used percent over its last span, 0–1 and oldest first, weighted
	-- across `accounts` ({ w, weight }) from the history runs the fetch attaches
	-- ([firstSeen, lastSeen, usedPercent, resetsAt]). Starts once every account
	-- has a reading; nil when any account has no runs, so the graph never pools
	-- fewer accounts than the ring.
	local function seriesOf(accounts: { any }, now: number): { number }?
		local tracked = {}
		local spanSecs = 0
		for _, a in accounts do
			local runs = val(a.w.runs)
			if type(runs) ~= "table" or #runs == 0 then
				return nil
			end
			table.insert(tracked, { runs = runs, weight = a.weight, next = 1, run = nil :: any })
			spanSecs = math.max(spanSecs, (tonumber(val(a.w.windowMinutes)) or 0) * 60)
		end
		if #tracked == 0 or spanSecs <= 0 then
			return nil
		end
		local out = {}
		for i = 0, SAMPLES - 1 do
			local t = now - spanSecs + spanSecs * i / (SAMPLES - 1)
			local sum, total, known = 0, 0, true
			for _, a in tracked do
				-- Samples only move forward: each account's run carries over.
				while a.next <= #a.runs and (tonumber(a.runs[a.next][1]) or math.huge) <= t do
					a.run = a.runs[a.next]
					a.next += 1
				end
				if a.run == nil then
					known = false
					break
				end
				local reset = tonumber(val(a.run[4]))
				sum += a.weight * (if reset and reset <= t then 0 else tonumber(a.run[3]) or 0)
				total += a.weight
			end
			if known and total > 0 then
				table.insert(out, math.floor(math.clamp(sum / total, 0, 100) * 10 + 0.5) / 1000)
			end
		end
		return if #out >= 2 then out else nil
	end

	-- One provider's widget bar views: Claude's and Codex's windows plan-weighted
	-- across every current account, the others' per window. `[id]` shows every
	-- window in its text and the shortest in its ring, bar and graph, toned by the
	-- worst; Claude and Codex add `[id-7d]` for the weekly window alone.
	local function usageViews(id: string, recs: { any }, now: number, out: { [string]: any })
		local ok, stale = {}, false
		for _, r in recs do
			if val(r.ok) == true and val(r.error) == nil then
				table.insert(ok, r)
				stale = stale or val(r.stale) == true
			end
		end
		if #ok == 0 then
			return
		end
		local pooled = id == "claude" or id == "codex"
		local windows = {}
		if pooled then
			for _, spec in POOLED_WINDOWS do
				local pool = if #ok > 1 then composite(ok, spec[1], now) else nil
				if pool then
					table.insert(windows, { label = spec[2], pct = pool.usedPercent, pace = pool.pacePct, reset = pool.nextReset, accounts = pool.accounts })
				else
					-- One account, or only one reporting this window.
					for _, r in ok do
						local w = windowOf(r, spec[1])
						if w then
							table.insert(windows, {
								label = spec[2],
								pct = tonumber(val(w.usedPercent)) or 0,
								pace = pacePercent(w, now),
								reset = epoch(w.resetsAt),
								accounts = { { w = w, weight = 1 } },
							})
							break
						end
					end
				end
			end
		else
			for _, slot in { "primary", "secondary" } do
				local w = val(ok[1][slot])
				if type(w) == "table" and val(w.usedPercent) ~= nil then
					table.insert(windows, {
						label = windowLabel(id, slot, w, ""),
						pct = tonumber(val(w.usedPercent)) or 0,
						pace = pacePercent(w, now),
						reset = epoch(w.resetsAt),
						accounts = { { w = w, weight = 1 } },
					})
				end
			end
		end
		local balance = balanceOf(ok[1])
		if #windows == 0 and balance == "" then
			return
		end
		-- The hover text: a line per window, then each account's own percents.
		local lines = if stale then { "Last known" } else {}
		local parts = {}
		local worst: string? = nil
		for _, w in windows do
			w.pct = math.clamp(w.pct, 0, 100)
			w.lvl = if stale then nil else level(w.pct, if w.pace >= 3 then w.pace else -1)
			worst = worse(worst, w.lvl)
			table.insert(parts, roundPct(w.pct))
			local line = w.label .. " " .. roundPct(w.pct)
			if not stale and w.reset and w.reset > now then
				line ..= " · resets in " .. span(w.reset - now)
			end
			table.insert(lines, line)
		end
		if balance ~= "" then
			table.insert(lines, balance)
		end
		if pooled and #recs > 1 then
			for i, r in recs do
				local each = {}
				if val(r.ok) == true and val(r.error) == nil then
					for _, spec in POOLED_WINDOWS do
						local w = windowOf(r, spec[1])
						table.insert(each, if w then roundPct(tonumber(val(w.usedPercent)) or 0) else "–")
					end
				else
					table.insert(each, "no reading")
				end
				local email = str(r.accountEmail)
				table.insert(lines, (if email ~= "" then email else "account " .. i) .. "  " .. table.concat(each, " · "))
			end
		end
		local title = table.concat(lines, "\n")
		local function graph(w: any): { number }?
			return if w and not stale then seriesOf(w.accounts, now) else nil
		end
		out[id] = {
			text = if #parts > 0 then table.concat(parts, " · ") else balance,
			tone = if stale then "muted" elseif worst then STATUS_TONE[worst] else nil,
			value = if windows[1] then windows[1].pct / 100 else nil,
			series = graph(windows[1]),
			title = title,
		}
		local weekly = if pooled then windows[#windows] else nil
		if weekly and weekly.label == "7d" then
			out[id .. "-7d"] = {
				text = "7d " .. roundPct(weekly.pct),
				tone = if stale then "muted" elseif weekly.lvl then STATUS_TONE[weekly.lvl] else nil,
				value = weekly.pct / 100,
				-- With only the weekly window reported, it is windows[1] too.
				series = if weekly == windows[1] then out[id].series else graph(weekly),
				title = title,
			}
		end
	end

	-- Widget views by widget id, for the enabled providers with a reading.
	usageWidgets = function(snap, now)
		local byId: { [string]: { any } } = {}
		local enabled = setting("providers")
		for _, rec in val(snap.providers) or {} do
			local id = str(rec.id)
			if table.find(enabled, id) then
				byId[id] = byId[id] or {}
				table.insert(byId[id], rec)
			end
		end
		local out = {}
		for id, recs in byId do
			usageViews(id, recs, now, out)
		end
		return out
	end
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

local function waitingList(field: string): { any }
	local view = store.waiting
	local list = if type(view) == "table" then val(view[field]) else nil
	return if type(list) == "table" then list else {}
end

local function waitingCounts(): any
	local view = store.waiting
	local counts = if type(view) == "table" then val(view.counts) else nil
	return if type(counts) == "table" then counts else {}
end

-- The wait Snooze 1d pushes: one on me first, then the earliest due. It skips the
-- ones `snooze` refuses for their kind or state (not started, done, my-time
-- ones, a job still running in time), so a new kind needs no change here; the
-- CLI also refuses one whose prompt the agent runs now. Second, the wait Retry
-- checks again: one the CLI put on me after its checks kept failing
-- (`retryable`), which nothing polls until then. (Inline, and one function for
-- both: the main chunk is at Luau's 200-local limit.)
local function firstDue(r: any): (any, any)
	local first, at, onMe, retry = nil, math.huge, false, nil
	for _, w in val(r.waits) or {} do
		local snoozable = w.state ~= "pending" and w.state ~= "done" and w.kind ~= "later"
			and not (w.kind == "job" and w.state == "waiting" and val(w.overdue) ~= true)
		if snoozable then
			local due = epoch(w.due_at) or math.huge
			local mine = w.ball == "on_me"
			if not first or (mine and not onMe) or (mine == onMe and due < at) then
				first, at, onMe = w, due, mine
			end
		end
		if not retry and val(w.retryable) == true then
			retry = w
		end
	end
	return first, retry
end

-- Per Tern pane, what the window half needs: the status segment, the session
-- card, and which panes its events matter for.
local function waitingPanes(): { [string]: any }
	local panes: { [string]: any } = {}
	local function add(pane: number?, r: any, session: string?, record: boolean)
		if pane then
			local wait, retry = firstDue(r)
			panes[tostring(pane)] = {
				text = str(r.status),
				on_me = r.ball == "on_me",
				outcome = val(r.outcome),
				unseen = val(r.unseen) == true,
				session = session,
				record = record,
				wait = if wait then str(wait.id) else nil,
				retry = if retry then str(retry.id) else nil,
			}
		end
	end
	local records: { [string]: boolean } = {}
	for _, p in waitingList("panes") do
		records[str(p.pane)] = true
		add(tonumber(val(p.pane)), p, nil, true)
	end
	-- A session in a pane that also has a pane record speaks for that pane.
	for _, s in waitingList("sessions") do
		if val(s.live) == true then
			add(tonumber(val(s.pane)), s, str(s.session_id), records[str(s.pane)] == true)
		end
	end
	return panes
end

-- Panes whose next command ends a job wait of this daemon's: the condition's,
-- else the record's own. Pane ids name the panes of the daemon a wait carries
-- (`daemon`): while another daemon ticks, only ours count; while this one ticks,
-- waits without the field count too, as the CLI takes them for the ticker's.
-- With no id to be had (`daemonKnown` without `daemonId`) every wait counts
-- unless another daemon ticks: the event goes without `--daemon`, and the CLI
-- scopes it by the daemon it runs under if it can tell, else not at all. Until
-- the id is known, a stamped one doesn't: such an unscoped event could end
-- another daemon's wait on a pane of the same number.
local function jobPanes(view: any): { [number]: boolean }
	local out: { [number]: boolean } = {}
	for _, field in { "sessions", "panes" } do
		for _, r in val(view[field]) or {} do
			for _, w in val(r.waits) or {} do
				local stamp = val(w.daemon) or val(r.daemon)
				local ours = if stamp ~= nil and (store.daemonId or not store.daemonKnown) then stamp == store.daemonId else not store.waitingSkipped
				if ours and w.kind == "job" and w.state == "waiting" then
					local cond = val(w.condition)
					local pane = (if type(cond) == "table" then tonumber(val(cond.pane)) else nil) or tonumber(val(r.pane))
					if pane then
						out[pane] = true
					end
				end
			end
		end
	end
	return out
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
		-- On the minute: the graphs and "resets in" lines move with the clock,
		-- and a summary rebuilt within the minute stays the one published.
		usage = if store.usage then usageWidgets(store.usage, os.time() // 60 * 60) else {},
		counts = counts,
		restore = restore,
		-- While another daemon ticks, the view's pane ids are its panes: none of ours.
		waiting = { on_me = tonumber(val(waitingCounts().on_me)) or 0, panes = if store.waitingSkipped then {} else waitingPanes() },
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

local function readWaiting(path: string): string
	local ok, raw = pcall(tern.fs.read, path, 16 * 1024 * 1024)
	return if ok then raw else ""
end

-- Republishes only what changed: kv.json is one file every VM rereads. `now`
-- and `health.checked_at` move on every tick; the warning line reads the
-- latter from `store.waiting`, rendered once a minute.
local function applyWaiting(view: any)
	store.waiting = view
	local at = view.now
	local health = val(view.health)
	local checked = if type(health) == "table" then health.checked_at else nil
	store.waitingUncheckedSince = if type(health) ~= "table" or val(checked) ~= nil
		then nil
		else store.waitingUncheckedSince or os.time()
	view.now = nil
	if checked then
		health.checked_at = nil
	end
	local sig = tern.json.encode(view)
	view.now = at
	if checked then
		health.checked_at = checked
	end
	if sig ~= store.waitingSig then
		store.waitingSig = sig
		tern.kv.set("waitingView", view)
		publishSummary()
		renderAll()
	end
end

local function publishActions(actions: any)
	store.waitingActions = actions
	local listed: { [string]: boolean } = {}
	for _, a in actions do
		listed[str(a.id)] = true
	end
	for id in store.waitingAcking do
		if not listed[id] then
			store.waitingAcking[id] = nil
		end
	end
	local sig = tern.json.encode(actions)
	if sig ~= store.waitingActionsSig then
		store.waitingActionsSig = sig
		tern.kv.set("waitingActions", actions)
	end
end

-- One `tick` at a time; a request during one runs right after it.
local function waitingTick()
	if not store.daemonKnown then
		return -- the timed tick runs once the daemon's id is known (end of file)
	end
	if store.waitingTicking then
		store.waitingAgain = true
		return
	end
	store.waitingTicking = true
	store.waitingAgain = false
	-- A leader window holds a lease (window.luau `leading`): only then can a
	-- resume be carried out, so only then does the CLI count down to giving one
	-- up. A lease runs LEASE_S on os.clock(); one further out is from before a
	-- reboot (or a wall-clock lease of an older build) and counts as none.
	local LEASE_S = 30 -- window.luau's LEASE_S: change both together
	local lease = tern.kv.get("waitingLeader")
	local left = if type(lease) == "table" and type(lease.expires) == "number" then lease.expires - os.clock() else 0
	local args = { "tick" }
	if left > 0 and left <= LEASE_S then
		table.insert(args, "--window")
	end
	if store.daemonId then
		table.insert(args, "--daemon")
		table.insert(args, store.daemonId)
	end
	python(WAITING_CLI, args, WAITING_TIMEOUT_MS, function(res)
		store.waitingTicking = false
		local okj, data = pcall(tern.json.decode, res.stdout)
		local nextIn = WAITING_RETRY_S
		local ticked = res.status == 0 and okj and type(data) == "table" and type(val(data.view)) == "table"
		local skipped = if ticked then str(data.skipped) else ""
		if ticked then
			-- Before the view goes out: while skipped, the summary leaves out the
			-- other daemon's panes (publishSummary).
			local wasSkipped, hadError = store.waitingSkipped, store.waitingError ~= nil
			store.waitingSkipped = if skipped ~= "" then skipped else nil
			store.waitingError = nil
			applyWaiting(val(data.view))
			-- Another daemon's windows carry the outbox out; ours would do it twice.
			publishActions(if skipped == "" and type(val(data.actions)) == "table" then val(data.actions) else tern.json.array())
			nextIn = tonumber(val(data.next_tick_s)) or nextIn
			if skipped ~= "" and wasSkipped == nil then
				tern.log.warn("codexbar: waiting tick skipped", skipped)
			end
			if (wasSkipped == nil) ~= (store.waitingSkipped == nil) then
				publishSummary()
				renderAll() -- the Waiting tab's line about the other daemon
			elseif hadError then
				renderAll() -- the warning line goes, though the view may be the same
			end
		else
			local detail = if okj and type(data) == "table" then str(data.error) else ""
			detail = if detail ~= "" then detail else firstLine(res.stderr)
			store.waitingError = if detail ~= "" then detail else "exit " .. res.status
			tern.log.warn("codexbar: waiting tick failed", store.waitingError)
			renderAll() -- the Waiting tab's warning line, or its empty state without a view
		end
		store.waitingRaw = readWaiting(WAITING_FILE)
		-- Another writer (the omp extension, tern-awui) may have written after the
		-- tick's last transaction: the poll would take that change as seen, and a
		-- window action it queued would wait for the next timed tick. Its view or
		-- an action the tick didn't return tells; one recheck, so no tick loop.
		-- A skipped tick publishes no action, so it doesn't look.
		local recheck = false
		if ticked and skipped == "" and not store.waitingAgain and not store.waitingRecheck then
			local okv, file = pcall(tern.json.decode, readWaiting(WAITING_VIEW_FILE))
			if okv and type(file) == "table" then
				file.now = nil
				if type(file.health) == "table" then
					file.health.checked_at = nil
				end
				recheck = tern.json.encode(file) ~= store.waitingSig
			end
			local listed: { [string]: boolean } = {}
			for _, a in (if type(store.waitingActions) == "table" then store.waitingActions else {}) do
				listed[str(a.id)] = true
			end
			local okr, state = pcall(tern.json.decode, store.waitingRaw)
			for _, a in (if okr and type(state) == "table" then val(state.actions) else nil) or {} do
				recheck = recheck or not listed[str(a.id)]
			end
			store.waitingAgain = recheck
		end
		store.waitingRecheck = recheck
		store.waitingAt = os.time() + nextIn
		store.waitingAtMono = os.clock() + nextIn
		if store.waitingAgain then
			waitingTick()
		end
	end)
end

-- Runs one CLI command. `done` gets its answer, nil after a failure the toast
-- reported. A view in the answer means the state changed, so the tick after it
-- publishes any window action the command queued.
local function waitingRun(cx: BlockCx?, what: string, args: { string }, done: ((any) -> ())?)
	python(WAITING_CLI, args, WAITING_TIMEOUT_MS, function(res)
		local okj, data = pcall(tern.json.decode, res.stdout)
		if res.status == 0 and okj and type(data) == "table" and val(data.ok) == true then
			local view = val(data.view)
			if type(view) == "table" then
				applyWaiting(view)
				waitingTick()
			end
			if done then
				done(data)
			end
			return
		end
		local detail = if okj and type(data) == "table" then str(data.error) else ""
		detail = if detail ~= "" then detail else firstLine(res.stderr)
		if cx then
			cx:toast("error", "CodexBar AgentView: " .. what .. " failed", detail)
		else
			tern.log.warn("codexbar: " .. what .. " failed", detail)
		end
		if done then
			done(nil)
		end
	end)
end

-- A window sees only its own panes; this half alone can tell that a pane is
-- gone from every window. A pane action's target gone: the outbox is the last
-- tick's, so an ack that landed stays marked until a tick drops its action, and
-- a failed one waits before the next try (while another daemon ticks, the outbox
-- here is empty). A watched job's pane gone without the event that ends its wait
-- (`tern close`, no window, a plugin reload): after 10 s, time for a
-- `pane_exited` in flight to land, the CLI hears `pane_closed`. Only this
-- daemon's job waits count (jobPanes). An empty listing is no evidence until
-- this daemon has listed a pane, as while it starts; after that, every pane is
-- gone, the last one closed too.
local function reconcilePanes()
	local live: { [number]: boolean } = {}
	for _, p in tern.pane.list() do
		live[p.pane] = true
		store.panesListed = true
	end
	if not store.panesListed then
		return
	end
	local now = os.clock()
	for _, a in (if type(store.waitingActions) == "table" then store.waitingActions else {}) do
		local pane = tonumber(val(a.pane))
		local id = str(a.id)
		if a.type ~= "toast" and pane and not live[pane] and now >= (store.waitingAcking[id] or 0) then
			store.waitingAcking[id] = math.huge
			waitingRun(nil, "ack", { "ack", "--action", id, "--result", "done" }, function(data)
				if not data and store.waitingAcking[id] then
					store.waitingAcking[id] = os.clock() + 30
				end
			end)
		end
	end
	-- A tick under way may be ending these waits already; its view decides after.
	if store.waitingTicking or type(store.waiting) ~= "table" then
		return
	end
	local watched = jobPanes(store.waiting)
	for pane in store.paneGoneAt do
		if live[pane] or not watched[pane] then
			store.paneGoneAt[pane] = nil
		end
	end
	for pane in watched do
		if not live[pane] then
			local since = store.paneGoneAt[pane]
			if not since then
				store.paneGoneAt[pane] = now
			elseif now - since >= 10 then
				store.paneGoneAt[pane] = math.huge
				local args = { "event", "--type", "pane_closed", "--pane", tostring(pane) }
				if store.daemonId then -- the CLI then ends only what this daemon's pane held
					table.insert(args, "--daemon")
					table.insert(args, store.daemonId)
				end
				waitingRun(nil, "pane event", args, function(data)
					if not data and store.paneGoneAt[pane] then
						store.paneGoneAt[pane] = os.clock() -- tries again in 10 s
					end
				end)
			end
		end
	end
end

-- Which pane runs which session, for the window half's set-aside. Only a
-- session or pane change asks for a tick: the applet rewrites agents.json every
-- 5 s, and each tick is a Python process.
local function noteAgents()
	local map: { [string]: any } = {}
	local parts = {}
	for _, a in (if type(store.agents) == "table" then val(store.agents.agents) else nil) or {} do
		local id = str(a.sessionId)
		local pane = tonumber(val(a.ternPane))
		if pane and id ~= "" then
			map[tostring(pane)] = { session = id, provider = str(a.provider) }
		end
		table.insert(parts, id .. ":" .. str(a.ternPane))
	end
	table.sort(parts)
	local sig = table.concat(parts, ",")
	if sig ~= store.waitingAgents then
		store.waitingAgents = sig
		tern.kv.set("paneSessions", map)
		waitingTick()
	end
end

-- `pane_exited` reaches only this half, which also runs with no window open.
-- A job can end right after its `add`, and its one event must not be lost: when
-- the last tick's view doesn't watch the pane, the view file decides, but only
-- if waiting.json changed since that tick. Most panes watch nothing, so this
-- decides before a Python process starts. A pane ending with its job sends
-- both events; the CLI took the first.
local function jobFinished(pane: number, status: number, exited: boolean)
	local now = os.clock()
	if exited and now - (store.jobEventAt[pane] or -math.huge) <= 2 then
		return
	end
	local watched = type(store.waiting) == "table" and jobPanes(store.waiting)[pane] == true
	if not watched and (store.waitingTicking or readWaiting(WAITING_FILE) ~= store.waitingRaw) then
		local okj, view = pcall(tern.json.decode, readWaiting(WAITING_VIEW_FILE))
		watched = okj and type(view) == "table" and jobPanes(view)[pane] == true
	end
	if watched then
		store.jobEventAt[pane] = now
		local args = { "event", "--type", "job_finished", "--pane", tostring(pane), "--status", tostring(status) }
		if store.daemonId then
			table.insert(args, "--daemon")
			table.insert(args, store.daemonId)
		end
		waitingRun(nil, "job event", args)
	end
end

tern.on("command_finished", function(ev: CommandFinishedEvent)
	jobFinished(ev.pane, ev.status, false)
end)
tern.on("pane_exited", function(ev: PaneExitedEvent)
	jobFinished(ev.pane, ev.status, true)
	store.jobEventAt[ev.pane] = nil
end)

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
-- A reading the source itself served late (`sourceStale`) stays last-known.
local function adoptCache(data: any)
	local now = os.time()
	for _, rec in val(data.providers) or {} do
		local at = epoch(val(rec.cachedAt))
		if val(rec.stale) == true and val(rec.sourceStale) ~= true and str(rec.refreshError) == ""
			and at and now - at <= staleAfter() + 30 then
			rec.stale = false
		end
	end
	-- The cache keeps no forecast or CLI version; carry the last fetch's. Rotation
	-- is read fresh from omp's state files even on a cache read, so it is not carried.
	local previous = store.usage
	if type(previous) == "table" then
		for _, k in { "forecast", "cliVersion" } do
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

-- FNV-1a, 32-bit; FNV's prime is 2^24 + 403, which keeps every product exact.
local function fnv1a(s: string): number
	local h = 2166136261
	for i = 1, #s do
		h = bit32.bxor(h, string.byte(s, i))
		h = (bit32.lshift(h, 24) + h * 403) % 4294967296
	end
	return h
end

-- A digest of what a cache read depends on besides the clock, as its last
-- answer listed it (codexbar_fetch.py's _cache_inputs): the cache, the CLI's
-- config, the account logins, Meridian's profiles and omp's rotation files.
-- Only the digest is kept, so login tokens don't stay in the daemon.
local function usageInputs(): string
	local parts = {}
	for _, path in store.cachePaths do
		local ok, raw = pcall(tern.fs.read, path, 4 * 1024 * 1024)
		table.insert(parts, if ok then #raw .. ":" .. fnv1a(raw) else "-")
	end
	return table.concat(parts, ",")
end

-- A settings reread is cache-only, including when the cache is stale.
-- `inputs` is a usageInputs() digest the caller already computed.
local function readUsageCache(allowFetch: boolean?, reclassify: boolean?, inputs: string?)
	if #setting("providers") == 0 then
		return
	end
	store.cacheInputs = inputs or usageInputs()
	store.cacheSpawnAt = os.time()
	store.cacheSeq += 1
	local seq = store.cacheSeq
	local generation = store.usageGeneration
	local previous = store.usage
	local args = providerArgs()
	table.insert(args, "--cache-only")
	python("codexbar_fetch.py", args, 30000, function(res)
		-- Reads overlap when one is slow, and an earlier one answers older
		-- files, so only the latest read counts, whatever it measured.
		if generation ~= store.usageGeneration or seq ~= store.cacheSeq then
			return
		end
		local okj, data = pcall(tern.json.decode, res.stdout)
		local inputs = if okj and type(data) == "table" then val(data.inputs) else nil
		if type(inputs) == "table" then
			local paths = {}
			for _, path in inputs do
				if type(path) == "string" then
					table.insert(paths, path)
				end
			end
			-- A changed list fails the next check once, which rereads against it.
			store.cachePaths = paths
		end
		local usable = res.status == 0 and okj and type(data) == "table"
		local cacheShown = type(store.usage) == "table" and val(store.usage.cacheOnly) == true
		local replaced = false
		if usable and (not store.fetching or allowFetch == false)
			and ((allowFetch == false and not reclassify) or not store.usage or measuredAt(data) > measuredAt(store.usage)
				or (reclassify and store.usage == previous and measuredAt(data) == measuredAt(store.usage))
				-- One cache read replaces another without a newer measurement: omp's
				-- rotation and the accounts the logins and the 24 h limit allow change
				-- on their own.
				or (allowFetch ~= false and cacheShown)) then
			adoptCache(data)
		elseif usable and not store.fetching and type(store.usage) == "table" then
			-- A newer fetch's reading stays, but omp's rotation is local and
			-- current, and an account whose login changed since is gone.
			store.usage.claudeRotation = data.claudeRotation
			store.usage.codexRotation = data.codexRotation
			local scopes = val(data.scopes)
			local records = val(store.usage.providers)
			if type(scopes) == "table" and type(records) == "table" then
				local kept = {}
				for _, rec in records do
					local scope = val(rec.sourceScope)
					local current = val(scopes[str(rec.id)])
					if type(scope) ~= "string" or type(current) ~= "string" or scope == current then
						table.insert(kept, rec)
					end
				end
				-- A Claude token refresh changes its login file just like a
				-- switch does; a fetch brings the account back either way.
				replaced = #kept < #records
				store.usage.providers = kept
			end
		end
		if allowFetch ~= false and (replaced or os.time() - math.max(store.usageAt, measuredAt(store.usage)) >= staleAfter()) then
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
		if pane == target then
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
				noteAgents()
				publishSummary()
				renderAll()
			end
			local updated = if type(store.agents) == "table" then tonumber(val(store.agents.updatedAt)) else nil
			if not updated or nowMs() - updated > AGENTS_STALE_MS then
				scanAgents(false)
			end
			reconcilePanes()
			-- Another writer (the omp extension, tern-awui) may have queued a window
			-- action. While another daemon ticks, the outbox is its business, but
			-- the view it writes still names this daemon's job waits: reconcilePanes
			-- and jobFinished read it, without a Python process.
			local raw = if store.waitingTicking then nil else readWaiting(WAITING_FILE)
			if raw ~= nil and raw ~= store.waitingRaw then
				if store.waitingSkipped then
					store.waitingRaw = raw
					local okv, view = pcall(tern.json.decode, readWaiting(WAITING_VIEW_FILE))
					if okv and type(view) == "table" and type(val(view.panes)) == "table" then
						applyWaiting(view)
					end
				else
					waitingTick()
				end
			end
			if store.menuRetryAt and os.clock() >= store.menuRetryAt and store.loadMenu then
				store.menuRetryAt = nil
				store.loadMenu()
			end
		end
		local poke = tern.kv.get("waitingPoke")
		if poke ~= nil and poke ~= store.lastPoke then
			store.lastPoke = poke
			waitingTick()
		end
		if not store.waitingTicking and (os.time() >= store.waitingAt or os.clock() >= store.waitingAtMono) then
			waitingTick()
		end
		local now = os.time()
		if now - store.cacheReadAt >= CACHE_EVERY_S then
			store.cacheReadAt = now
			local inputs = if now - store.cacheSpawnAt >= CACHE_FORCE_S then nil else usageInputs()
			if inputs == nil or inputs ~= store.cacheInputs then
				readUsageCache(nil, nil, inputs)
			elseif os.time() - math.max(store.usageAt, measuredAt(store.usage)) >= staleAfter() then
				-- What the skipped read would have done after reading.
				fetchUsage(true)
			end
		end
		-- Ages and countdowns move once a minute, the widgets' among them.
		if ticks % 120 == 0 then
			publishSummary()
			renderAll()
		end
	end)
	if not ok then
		tern.log.error("codexbar:", err)
	end
	tern.timer(TICK_MS, tick)
end

-- Filtering (AgentsSection.qml, Filter.js) ----------------------------------
-- A query is words separated by spaces, in any order. A session matches when every word matches one of
-- its fields or recent turns. A word matches a field as a substring, else as prefixes of consecutive words
-- ("cmh" finds "Check MCP history", "autor" finds "Auto-Research"); a turn only as a substring, so long
-- prose cannot satisfy a short word by chance.

local function isWordByte(b: number): boolean
	return (b >= 48 and b <= 57) or (b >= 65 and b <= 90) or (b >= 97 and b <= 122) or b >= 128
end

-- Whether byte k begins a word: after a non-word byte or at a camelCase boundary.
local function wordStart(text: string, k: number): boolean
	local c = string.byte(text, k)
	if not c or not isWordByte(c) then
		return false
	end
	if k == 1 then
		return true
	end
	local p = string.byte(text, k - 1)
	if not isWordByte(p) then
		return true
	end
	return c >= 65 and c <= 90 and p >= 97 and p <= 122
end

local lastQuery: string?, lastWords: { string } = nil, {}

-- The query's distinct lowercase words; the last answer is kept, as every row asks for the same query.
local function queryWords(q: string): { string }
	if q ~= lastQuery then
		local out = {}
		for w in string.gmatch(string.lower(q), "%S+") do
			if not table.find(out, w) then
				table.insert(out, w)
			end
		end
		lastQuery, lastWords = q, out
	end
	return lastWords
end

-- Matched byte positions of lowercase `word` in `text`, or nil; `initials` allows the
-- consecutive-word-prefix form.
local function matchPositions(text: string, word: string, initials: boolean): { number }?
	if text == "" or word == "" then
		return nil
	end
	local hay = string.lower(text)
	local at = string.find(hay, word, 1, true)
	if at then
		local run = {}
		for r = 0, #word - 1 do
			table.insert(run, at + r)
		end
		return run
	end
	if not initials then
		return nil
	end
	-- Each matched byte continues the run, or starts the word right after it; a word skipped in between
	-- breaks the match. Failed (start, word index) states are memoized.
	local failed: { [number]: boolean } = {}
	local function walk(from: number, j: number): { number }?
		if j > #word then
			return {}
		end
		local key = from * (#word + 1) + j
		if failed[key] then
			return nil
		end
		local want = string.byte(word, j)
		local function try(k: number): { number }?
			if k <= #hay and string.byte(hay, k) == want then
				local rest = walk(k + 1, j + 1)
				if rest then
					table.insert(rest, 1, k)
					return rest
				end
			end
			return nil
		end
		if j == 1 then
			for k = 1, #hay do
				if wordStart(text, k) then
					local found = try(k)
					if found then
						return found
					end
				end
			end
		else
			local found = try(from)
			if found then
				return found
			end
			-- The next word: the first to start at or after `from` (tried above when it starts there).
			for k = from, #hay do
				if wordStart(text, k) then
					if k > from then
						found = try(k)
						if found then
							return found
						end
					end
					break
				end
			end
		end
		failed[key] = true
		return nil
	end
	return walk(1, 1)
end

type Hit = { word: string, run: { number } }

-- The words that match `text`, each with its positions, or nil for none.
local function hits(text: string, words: { string }, initials: boolean): { Hit }?
	local out: { Hit } = {}
	for _, w in words do
		local run = matchPositions(text, w, initials)
		if run then
			table.insert(out, { word = w, run = run })
		end
	end
	return if #out > 0 then out else nil
end

local function hitSet(found: { Hit }?): { [number]: boolean }
	local set = {}
	for _, h in found or {} do
		for _, k in h.run do
			set[k] = true
		end
	end
	return set
end

-- Bytes `from`..`to` of `text` as spans, the hit bytes marked as a search match.
local function marked(text: string, hit: { [number]: boolean }, from: number, to: number): { Span | string }
	local out: { Span | string } = {}
	local k = from
	while k <= to do
		local on = hit[k] == true
		local e = k
		while e < to and (hit[e + 1] == true) == on do
			e += 1
		end
		local part = string.sub(text, k, e)
		table.insert(out, if on then ui.span(part, "mark strong") else part)
		k = e + 1
	end
	return out
end

-- `text` as spans, the bytes every filter word matched marked.
local function highlighted(text: string, q: string): { Span | string }
	return marked(text, hitSet(hits(text, queryWords(q), true)), 1, #text)
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

local function isContinuation(text: string, k: number): boolean
	local b = string.byte(text, k) or 0
	return b >= 128 and b < 192
end

-- Spans around the `focus` hits (a window per contiguous piece of a hit, so a long word between initials is
-- cut; overlapping windows merged, the text between them elided), with every hit in `found` marked. Each
-- part is clamped to two lines, so the windows share one budget: each starts just before its piece and
-- shrinks as the part holds more pieces.
local function excerpt(text: string, found: { Hit }, focus: { Hit }): { Span | string }
	local pieces: { { number } } = {}
	for _, h in focus do
		local start = h.run[1]
		for i = 2, #h.run + 1 do
			if i > #h.run or h.run[i] ~= h.run[i - 1] + 1 then
				table.insert(pieces, { start, h.run[i - 1] })
				start = h.run[i]
			end
		end
	end
	local before, after = math.floor(24 / #pieces), math.floor(96 / #pieces)
	local windows = {}
	for _, p in pieces do
		local from = math.max(1, p[1] - before)
		local to = math.min(#text, p[2] + after)
		-- Keep cuts on UTF-8 boundaries.
		while from > 1 and isContinuation(text, from) do
			from -= 1
		end
		while to < #text and isContinuation(text, to + 1) do
			to += 1
		end
		table.insert(windows, { from, to })
	end
	table.sort(windows, function(a, b)
		return a[1] < b[1]
	end)
	local merged = { windows[1] }
	for i = 2, #windows do
		local last = merged[#merged]
		if windows[i][1] <= last[2] + 1 then
			last[2] = math.max(last[2], windows[i][2])
		else
			table.insert(merged, windows[i])
		end
	end
	local hit = hitSet(found)
	local out: { Span | string } = {}
	if merged[1][1] > 1 then
		table.insert(out, "… ")
	end
	for i, w in merged do
		if i > 1 then
			table.insert(out, " … ")
		end
		for _, s in marked(text, hit, w[1], w[2]) do
			table.insert(out, s)
		end
	end
	if merged[#merged][2] < #text then
		table.insert(out, " …")
	end
	return out
end

local function recordMatches(r: any, q: string): boolean
	local words = queryWords(q)
	if #words == 0 then
		return true
	end
	local fields: { string } = {}
	for _, f in FIELDS do
		table.insert(fields, fieldText(r, f.name))
	end
	local turns: { string } = {}
	for _, t in val(r.recent) or {} do
		if t.kind ~= "tools" then
			table.insert(turns, string.lower(oneLine(t.text)))
		end
	end
	for _, w in words do
		local ok = false
		for _, text in fields do
			if matchPositions(text, w, true) then
				ok = true
				break
			end
		end
		if not ok then
			for _, text in turns do
				if string.find(text, w, 1, true) then
					ok = true
					break
				end
			end
		end
		if not ok then
			return false
		end
	end
	return true
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

local function hasWords(found: { Hit }, of: { Hit }): boolean
	for _, x in of do
		local ok = false
		for _, y in found do
			ok = ok or y.word == x.word
		end
		if not ok then
			return false
		end
	end
	return true
end

-- Where every filter word matched beyond the row's label: each matched field the label doesn't show, then
-- the turns holding words nothing before showed (else the first matching turn when nothing else is shown).
-- The prompt is left out when a chosen user turn repeats it with the same words. Each part's excerpt
-- centers on the words no earlier part (or the label) showed. Tool-call summaries are tool names, not
-- conversation: a filter never matches them.
type SnippetPart = { label: string?, user: boolean, spans: { Span | string } }
local function filterSnippet(r: any, q: string): { SnippetPart }?
	local words = queryWords(q)
	if #words == 0 then
		return nil
	end
	local shown = labelField(r)
	local seen: { [string]: boolean } = {}
	local inView: { [string]: boolean } = {} -- words the label, then each part so far, shows
	local fields = {}
	for _, f in FIELDS do
		local text = fieldText(r, f.name)
		local found = hits(text, words, true)
		if found then
			for _, h in found do
				seen[h.word] = true
				if f.name == shown then
					inView[h.word] = true
				end
			end
			if f.name ~= shown then
				table.insert(fields, { name = f.name, label = f.label, text = text, found = found })
			end
		end
	end
	local turns = {}
	for _, t in val(r.recent) or {} do
		if t.kind ~= "tools" then
			local text = oneLine(t.text)
			table.insert(turns, { user = t.role == "user", text = text, found = hits(text, words, false), chosen = false })
		end
	end
	local count = 0
	for _, t in turns do
		if t.found then
			local fresh = false
			for _, h in t.found do
				fresh = fresh or not seen[h.word]
			end
			if fresh then
				t.chosen = true
				count += 1
				for _, h in t.found do
					seen[h.word] = true
				end
			end
		end
	end
	if count == 0 and #fields == 0 then
		for _, t in turns do
			if t.found then
				t.chosen = true
				break
			end
		end
	end
	local parts = {}
	for _, f in fields do
		local repeated = false
		if f.name == "lastPrompt" then
			for _, t in turns do
				if t.chosen and t.user and string.sub(t.text, 1, 100) == string.sub(f.text, 1, 100) and hasWords(t.found, f.found) then
					repeated = true
				end
			end
		end
		if not repeated then
			table.insert(parts, { label = f.label, user = false, text = f.text, found = f.found })
		end
	end
	for _, t in turns do
		if t.chosen then
			table.insert(parts, { user = t.user, text = t.text, found = t.found })
		end
	end
	if #parts == 0 then
		return nil
	end
	local out: { SnippetPart } = {}
	for _, p in parts do
		local fresh: { Hit } = {}
		for _, h in p.found do
			if not inView[h.word] then
				table.insert(fresh, h)
			end
		end
		for _, h in p.found do
			inView[h.word] = true
		end
		table.insert(out, { label = p.label, user = p.user, spans = excerpt(p.text, p.found, if #fresh > 0 then fresh else p.found) })
	end
	return out
end

-- Agents and History lists ---------------------------------------------------

-- The recent turns as a chat: the user's on the right, the agent's and its tool calls under its name.
local function turnNodes(r: any): { Node }
	local out: { Node } = {}
	local prev = nil
	for i, t in val(r.recent) or {} do
		local user = t.role == "user"
		local body = if t.kind == "tools" then ui.text({ ui.span("⚙ " .. oneLine(t.text), "muted") }) else ui.text({ ui.span(oneLine(t.text)) })
		table.insert(out, ui.node("message", {
			key = "t" .. i,
			side = if user then "end" else "start",
			author = if user then nil else AGENT_NAMES[str(r.provider)] or str(r.provider),
			continued = prev ~= nil and prev == user,
		}, { body }))
		prev = user
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
		if own == pane then
			if hit then
				return nil
			end
			hit = recordKey(r)
		end
	end
	return hit
end

type Row = { key: string, record: any, history: boolean, waiting: boolean? }

-- Over a day in a state other than working or blocked: the tab's eye switch hides these.
local function isOld(a: any, nowMs: number): boolean
	local since = tonumber(val(a.stateChangedAt)) or 0
	return since > 0 and nowMs - since > 86400000 and a.state ~= "working" and a.state ~= "blocked"
end

local function oldCount(): number
	local nowMs = os.time() * 1000
	local n = 0
	for _, a in liveAgents() do
		if isOld(a, nowMs) then
			n += 1
		end
	end
	return n
end

-- Agents grouped by folder (alphabetical), newest state change first. A filter searches old sessions too.
local function agentRows(q: string): { { folder: string, rows: { any } } }
	local groups: { [string]: { any } } = {}
	local order: { string } = {}
	local hideOld = #queryWords(q) == 0 and setting("hideOld")
	local nowMs = os.time() * 1000
	for _, a in liveAgents() do
		if recordMatches(a, q) and not (hideOld and isOld(a, nowMs)) then
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

local BALLS = {
	{ id = "on_me", title = "On me", rank = { attention = 1, overdue = 2, fulfilled = 3 } },
	{ id = "on_machine", title = "Machine watching" },
	{ id = "on_timer", title = "On a timer" },
	{ id = "on_someone", title = "Waiting on something" },
	{ id = "on_my_time", title = "When I have time" },
}

local function waitKey(r: any): string
	local id = str(r.session_id)
	return if id ~= "" then "s-" .. nodeKey(id) else "p-" .. str(r.pane)
end

local function waitLive(r: any): boolean
	return str(r.session_id) == "" or val(r.live) == true
end

local function waitTitle(r: any): string
	local title = oneLine(if str(r.session_id) ~= "" then r.title else r.label)
	return if title ~= "" then title else "Pane " .. str(r.pane)
end

-- What the row waits for: each thread's current step from the CLI (`threads`), with
-- their marks when there are several. A free-text wait has a line of its own
-- (`waiting_for`), so it is left out here. `rest` leaves out the first thread,
-- the one the row's headline already tells, and keeps the others' marks.
local function waitSummary(r: any, rest: boolean?): string
	local own = oneLine(r.waiting_for)
	local threads: { string } = {}
	for i, t in val(r.threads) or {} do
		local text = oneLine(t)
		if not (rest and i == 1) and (own == "" or string.sub(text, -#own) ~= own) then
			table.insert(threads, text)
		end
	end
	if rest then
		return table.concat(threads, "  ·  ")
	end
	if #threads == 1 then
		return (string.gsub(threads[1], "^[^ ]+ ", "", 1)) -- the row's own mark says the outcome
	end
	if #threads > 1 then
		return table.concat(threads, "  ·  ")
	end
	return if own ~= "" then "" else oneLine(r.status)
end

local function waitMatches(r: any, q: string): boolean
	local words = queryWords(q)
	if #words == 0 then
		return true
	end
	local fields = { waitTitle(r), oneLine(r.note), oneLine(r.project), waitSummary(r), oneLine(r.waiting_for), oneLine(r.headline) }
	for _, w in words do
		local ok = false
		for _, text in fields do
			if matchPositions(text, w, true) then
				ok = true
				break
			end
		end
		if not ok then
			return false
		end
	end
	return true
end

-- The view's order within each group, sessions before panes; a group with a
-- `rank` sorts by outcome first, in that same order within each. A render asks
-- twice (the selection, the list), so the last answer is kept per view and query.
local function waitingGroups(q: string): { { ball: any, rows: { any } } }
	local memo = store.waitingMemo
	if memo and memo.view == store.waiting and memo.q == q then
		return memo.out
	end
	local out = {}
	for _, ball in BALLS do
		local rows = {}
		for _, field in { "sessions", "panes" } do
			for _, r in waitingList(field) do
				if r.ball == ball.id and waitMatches(r, q) then
					table.insert(rows, r)
				end
			end
		end
		if ball.rank then
			local order: { [any]: number } = {}
			for i, r in rows do
				order[r] = (ball.rank[r.outcome] or 4) * #rows + i
			end
			table.sort(rows, function(x, y)
				return order[x] < order[y]
			end)
		end
		if #rows > 0 then
			table.insert(out, { ball = ball, rows = rows })
		end
	end
	store.waitingMemo = { view = store.waiting, q = q, out = out }
	return out
end

local function selectable(b: Block): { Row }
	local q = string.lower(b.filter)
	local out: { Row } = {}
	if b.tab == "agents" then
		for _, g in agentRows(q) do
			for _, r in g.rows do
				table.insert(out, { key = recordKey(r), record = r, history = false })
			end
		end
	elseif b.tab == "waiting" then
		for _, g in waitingGroups(q) do
			for _, r in g.rows do
				table.insert(out, { key = waitKey(r), record = r, history = false, waiting = true })
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

-- The pane a row's session card floats over, nil when the row has no Card button:
-- a live omp session in Tern on Agents, a live row with a pane on Waiting.
local function cardPane(r: any, waiting: boolean): number?
	if waiting then
		return if waitLive(r) then tonumber(val(r.pane)) else nil
	end
	return if r.provider == "omp" then tonumber(val(r.ternPane)) else nil
end

-- A capped list scrolls itself and keeps the selected row in view. The cap is
-- larger than any pane; codexbar.css shrinks the list to the height left under
-- the tabs and filter (blocks report 80×24 cells whatever their size, still in
-- Tern 0.9).
local LIST_MAX = { lines = 1000 }
-- Node path of list rows: the `fill` box in the main region, then the list.
local LIST_PATH = "main.fill.list."

local function peekNode(b: Block, r: any, history: boolean): Node
	local kids: { Node } = {}
	local recap = str(r.recap)
	if recap ~= "" then
		table.insert(kids, withp(ui.text({ ui.span((if val(r.recapStale) == true then "Earlier: " else "") .. recap, "muted") }), { key = "recap" }))
	end
	if history then
		local where = { ui.span(shortPath(r.cwd), "path"), ui.span("  " .. str(r.host), "muted") }
		table.insert(kids, withp(ui.text(where), { key = "where" }))
	end
	local key = recordKey(r)
	local buttons: { Node } = {}
	if history then
		if LAUNCH_HOSTS[str(r.host)] and str(r.resumeCommand) ~= "" then
			local busy = b.launched[key] ~= nil and os.clock() - b.launched[key] < 60
			table.insert(buttons, button("Launch", "launch", { variant = "primary", keys = { "enter" }, busy = busy }))
		end
		if str(r.resumeCommand) ~= "" then
			table.insert(buttons, button("Copy resume command", "copy_resume", { icon = "copy" }))
		end
		table.insert(buttons, button("Dismiss", "dismiss", { keys = { "del" } }))
	else
		table.insert(buttons, button("Focus", "focus", { variant = "primary", keys = { "enter" } }))
		if cardPane(r, false) then
			table.insert(buttons, button("Card", "card", { keys = { "shift", "enter" } }))
		end
		local ternInstalled = type(store.agents) == "table" and val(store.agents.ternInstalled) == true
		if ternInstalled and r.host == "kitty" and str(r.resumeCommand) ~= "" then
			table.insert(buttons, button("Teleport to Tern", "teleport", {
				icon = "tern",
				busy = b.busy == key,
				disabled = if r.state == "idle" then false else "Only idle sessions can move",
			}))
		end
		if str(r.resumeCommand) ~= "" then
			table.insert(buttons, button("Copy resume command", "copy_resume", { icon = "copy" }))
		end
	end
	-- Once a filter is being typed, space is a filter character, not the peek key.
	local peekKeys = if b.filter == "" then { "space" } else nil
	table.insert(buttons, button(if b.peek then "Hide conversation" else "Conversation", "peek", { icon = "message", keys = peekKeys, on = b.peek }))
	table.insert(kids, withp(ui.row(buttons), { key = "actions", wrap = true }))
	if b.peek then
		local turns = turnNodes(r)
		if #turns > 0 then
			table.insert(kids, ui.node("scroll", { key = "conv", role = "codexbar.conv", stick = "end", max = { h = "16lines" } }, turns))
		else
			table.insert(kids, ui.node("empty", { key = "conv", variant = "inline", icon = "message", title = "No recent turns" }))
		end
	end
	-- Keyed by the session, so each conversation keeps its own scroll position.
	return withp(ui.col(kids), { key = "peek-" .. key, role = "codexbar.peek", gap = "sm" })
end

local function agentItem(b: Block, r: any, focused: boolean, now: number, q: string): Node
	local label: { Span | string } = { stateGlyph(r, focused, now), " " }
	for _, s in highlighted(fieldText(r, labelField(r)), q) do
		table.insert(label, s)
	end
	local idleFor = now - (tonumber(val(r.stateChangedAt)) or now)
	return ui.node("item", {
		key = recordKey(r),
		label = label,
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
	local title = highlighted(fieldText(r, labelField(r)), q)
	local launchable = LAUNCH_HOSTS[str(r.host)] and str(r.resumeCommand) ~= ""
	return ui.node("item", {
		key = recordKey(r),
		label = title,
		detail = cwdLabel(r.cwd),
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

-- Where the row matched, under it in the conversation panel's look: matched fields as labelled lines, matched
-- turns as messages. Nil while not filtering or when nothing beyond the label matched.
local function snippetNode(r: any, q: string): Node?
	local parts = filterSnippet(r, q)
	if not parts then
		return nil
	end
	local kids: { Node } = {}
	local prev = nil
	for i, p in parts do
		local spans: { Span | string } = {}
		if p.label then
			table.insert(spans, ui.span(p.label .. ": ", "muted"))
		end
		for _, s in p.spans do
			table.insert(spans, s)
		end
		local body = ui.node("text", { spans = spans, lines = 2 })
		if p.label then
			table.insert(kids, withp(body, { key = "f" .. i }))
			prev = nil
		else
			table.insert(kids, ui.node("message", {
				key = "t" .. i,
				side = if p.user then "end" else "start",
				author = if p.user then nil else AGENT_NAMES[str(r.provider)] or str(r.provider),
				continued = prev ~= nil and prev == p.user,
			}, { body }))
			prev = p.user
		end
	end
	return withp(ui.col(kids), { key = "snip-" .. recordKey(r), role = "codexbar.snippet", gap = "sm" })
end

local function addRow(kids: { Node }, row: Node, r: any, q: string)
	table.insert(kids, row)
	local snip = snippetNode(r, q)
	if snip then
		table.insert(kids, snip)
	end
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

local function listView(b: Block, children: { Node }, empty: Node): Node
	if #children == 0 then
		return withp(empty, { key = "list" }) -- the list's key: empty, loading and rows take turns in one place
	end
	local sel = selected(b)
	-- No `filter` prop: Tern would mark its own fuzzy match of the whole query; the rows mark each word.
	return ui.node("list", {
		key = "list",
		selected = if sel then LIST_PATH .. sel.key else nil,
		max = LIST_MAX,
	}, children)
end

-- An inline empty state; `act` adds its one quiet button.
local function emptyNode(icon: string, title: string, text: string?, act: { [string]: any }?): Node
	local kids: { Node } = {}
	if act then
		table.insert(kids, button(act.text, act.act, { variant = "quiet", keys = act.keys }))
	end
	return ui.node("empty", { variant = "inline", frame = true, icon = icon, title = title, text = text }, kids)
end

local function noMatch(b: Block, what: string): Node
	return emptyNode("search", "No " .. what .. " matches “" .. b.filter .. "”", nil, { text = "Clear", act = "clear", keys = { "esc" } })
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
			addRow(kids, agentItem(b, r, key == focus, now, q), r, q)
			if sel and sel.key == key then
				table.insert(kids, peekNode(b, r, false))
			end
		end
	end
	local hidden = if b.filter == "" and setting("hideOld") then oldCount() else 0
	local empty = if b.filter ~= "" then noMatch(b, "session")
		elseif hidden > 0 then emptyNode("eye-off", "All " .. hidden .. " sessions are older than a day", nil, { text = "Show them", act = "hide_old", keys = { "tab" } })
		else emptyNode("terminal", "No coding agents are running", "Sessions show up here as they start.")
	return listView(b, kids, empty)
end

local function historyView(b: Block, now: number): Node
	local q = string.lower(b.filter)
	local sel = selected(b)
	local reboot, exited = historyRows(q)
	local kids: { Node } = {}
	local function add(r)
		local key = recordKey(r)
		addRow(kids, historyItem(b, r, now, q), r, q)
		if sel and sel.key == key then
			table.insert(kids, peekNode(b, r, true))
		end
	end
	if #reboot > 0 then
		table.insert(kids, groupHead("h-reboot", { ui.span("Interrupted by restart", "strong"), ui.span("  " .. #reboot, "muted") }, {
			button("Restore all", "restore_all", { icon = "repeat" }),
			button("Dismiss all", "dismiss_all", { variant = "quiet" }),
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
	local empty = if b.filter ~= "" then noMatch(b, "closed session")
		else emptyNode("history", "No closed sessions yet", "Sessions you close show up here.")
	return listView(b, kids, empty)
end

-- "overdue", time left, else how long it has been set aside.
local function waitingTime(r: any, now: number): (string, string)
	for _, w in val(r.waits) or {} do
		if val(w.overdue) == true then
			return "overdue", "error"
		end
	end
	local due = epoch(r.due_at)
	if due then
		if due <= now then
			return "due", "warning"
		end
		return leftUntil(due, now), "muted"
	end
	local since = epoch(r.set_aside_at)
	return if since then ageFrom(since * 1000, now * 1000) else "", "dim"
end

-- A do block: the main chunk is at Luau's 200-local limit.
local addWaitingRow: (kids: { Node }, r: any, now: number, q: string) -> ()
do
	local OUTCOMES = {
		fulfilled = { prefix = "✅ ", tone = "success" },
		attention = { prefix = "⚠️ ", tone = "warning" },
		overdue = { prefix = "⏰ ", tone = "warning" },
	}

	-- The row, then what happened under it, in the outcome's tone.
	addWaitingRow = function(kids, r, now, q)
		local onMe = r.ball == "on_me"
		local outcome = OUTCOMES[r.outcome]
		local label: { Span | string } = {}
		if outcome then
			table.insert(label, ui.span(outcome.prefix, outcome.tone))
		elseif onMe or val(r.unseen) == true then
			table.insert(label, ui.span("● ", "warning"))
		end
		-- The project leads: session titles alone don't tell sessions apart.
		local project = oneLine(r.project)
		if project == "" then
			project = cwdLabel(r.cwd)
		end
		if project ~= "" then
			for _, s in highlighted(project, q) do
				table.insert(label, if type(s) == "string" then ui.span(s, "accent strong") else s)
			end
			table.insert(label, "  ")
		end
		for _, s in highlighted(waitTitle(r), q) do
			table.insert(label, s)
		end
		-- The label carries the outcome's mark, so its line under the row doesn't repeat it.
		local headline = if outcome then oneLine(r.headline) else ""
		if outcome and string.sub(headline, 1, #outcome.prefix) == outcome.prefix then
			headline = string.sub(headline, #outcome.prefix + 1)
		end
		local time, token = waitingTime(r, now)
		-- The headline tells the first thread; the others still show beside it.
		local detail = waitSummary(r, headline ~= "")
		table.insert(kids, ui.node("item", {
			key = waitKey(r),
			label = label,
			detail = if detail ~= "" then detail else nil,
			value = { ui.span(string.format("%7s", time), token) },
			icon = if str(r.session_id) ~= "" then AGENT_ICONS.omp else "terminal",
			tone = if outcome then outcome.tone elseif onMe then "warning" else nil,
			role = "codexbar.waiting",
		}))
		if headline ~= "" then
			table.insert(kids, withp(ui.text({ ui.span(clip(headline, 100), outcome.tone) }), { key = "hl-" .. waitKey(r), role = "codexbar.headline" }))
		end
	end
end

local function detailLines(key: string, head: string, lines: { Node }): Node
	local kids: { Node } = { ui.text({ ui.span(head, "muted") }) }
	for _, n in lines do
		table.insert(kids, n)
	end
	return withp(ui.lines(kids), { key = key })
end

-- The same colors as the session card's badges in the window half.
local PR_STATE_TOKEN = { open = "success", draft = "muted", merged = "info", closed = "error" }

-- The session's PRs as links, in the CLI's order: the ones it waits on or created, each saying
-- which, or with `mentioned` the ones it only mentions, muted.
local function prNodes(r: any, mentioned: boolean): { Node }
	local out: { Node } = {}
	for _, pr in val(r.prs) or {} do
		if (pr.source == "mentioned") ~= mentioned then
			continue
		end
		local text = "PR#" .. str(pr.number) .. " " .. clip(oneLine(val(pr.title) or pr.repo), 60)
		local spans: { any } = { ui.link(text, str(pr.url), if mentioned then "muted" else nil) }
		local source = if pr.source == "wait" then "waiting on" elseif pr.source == "created" then "created" else ""
		if source ~= "" then
			table.insert(spans, ui.span("  " .. source, "dim"))
		end
		local state = str(pr.state)
		if state ~= "" then
			table.insert(spans, ui.span("  " .. state, PR_STATE_TOKEN[state] or "muted"))
		end
		table.insert(out, withp(ui.text(spans), { key = "pr-" .. nodeKey(str(pr.repo)) .. "-" .. str(pr.number), role = "codexbar.pr" }))
	end
	return out
end

-- The selected row: my sentence and the buttons; Space adds the rest of the record.
local function waitingPeek(b: Block, r: any, now: number): Node
	local kids: { Node } = {}
	local note = oneLine(r.note)
	if note ~= "" then
		table.insert(kids, withp(ui.text({ ui.span(note, "strong") }), { key = "note" }))
	end
	local prs = prNodes(r, false)
	if #prs > 0 then
		table.insert(kids, detailLines("prs", "Pull requests", prs))
	end
	local mentioned = prNodes(r, true)
	if #mentioned > 0 then
		table.insert(kids, detailLines("mentioned", "Mentioned", mentioned))
	end
	local rowKey = waitKey(r)
	local live = waitLive(r)
	local buttons: { Node } = {}
	if live then
		table.insert(buttons, button("Open", "wait_open", { variant = "primary", keys = { "enter" } }))
	else
		local busy = b.busy == rowKey or (b.launched[rowKey] ~= nil and os.clock() - b.launched[rowKey] < 60)
		table.insert(buttons, button("Resume", "wait_open", { variant = "primary", keys = { "enter" }, busy = busy }))
	end
	local due, retry = firstDue(r)
	if retry then -- any of the row's waits, not only the one its headline tells: each is a dead end without it
		table.insert(buttons, button("Retry", "wait_retry", { icon = "repeat", keys = { "f5" }, title = "Check “" .. oneLine(retry.label) .. "” again now" }))
	end
	table.insert(buttons, button("Snooze 1d", "wait_snooze", {
		keys = if b.filter == "" then { "+" } else nil,
		disabled = if due then false else "Nothing to snooze",
	}))
	table.insert(buttons, button("Done", "wait_done", { keys = { "del" } }))
	if cardPane(r, true) then
		table.insert(buttons, button("Card", "wait_card", { keys = { "shift", "enter" } }))
	end
	table.insert(buttons, button("Manual wait…", "wait_what", { keys = { "f2" } }))
	local peekKeys = if b.filter == "" then { "space" } else nil
	table.insert(buttons, button(if b.peek then "Hide details" else "Details", "peek", { icon = "message", keys = peekKeys, on = b.peek }))
	table.insert(kids, withp(ui.row(buttons), { key = "actions", wrap = true }))
	if b.peek then
		local waitingFor = oneLine(r.waiting_for)
		if waitingFor ~= "" then -- in full: the row's own line clips it at 100 characters
			table.insert(kids, detailLines("waiting-for", "Waiting for", { ui.text(waitingFor) }))
		end
		local suggested = oneLine(r.suggested_note)
		if suggested ~= "" then
			table.insert(kids, withp(ui.row({
				ui.text({ ui.span("Suggested: ", "muted"), suggested }),
				button("Accept", "wait_accept"),
			}), { key = "suggested" }))
		end
		local history = {}
		for i, h in val(r.history) or {} do
			local at = epoch(h.at)
			table.insert(history, withp(ui.text({
				ui.span((if at then absoluteTime(at, now) else "") .. "  ", "dim"),
				ui.span(oneLine(h.text), if h.kind == "fired" then "success" else nil),
			}), { key = "h" .. i }))
		end
		if #history > 0 then
			table.insert(kids, detailLines("since", "Since set aside", history))
		end
		local nextStep = oneLine(r.next_step)
		if nextStep ~= "" then
			table.insert(kids, detailLines("next", "Next step", { ui.text(nextStep) }))
		end
		local recap = oneLine(r.recap)
		if recap ~= "" then
			local at = epoch(r.recap_at)
			local head = (if val(r.recap_stale) == true then "Earlier recap" else "Recap") .. (if at then " · " .. absoluteTime(at, now) else "")
			table.insert(kids, detailLines("recap", head, { ui.text({ ui.span(recap, "muted") }) }))
		end
	end
	return withp(ui.col(kids), { key = "peek-" .. rowKey, role = "codexbar.peek", gap = "sm" })
end

-- A line above the groups while the waits may be out of date: the last tick
-- failed (this half saw it, or the CLI noted it in `health`), no tick finished
-- for 15 minutes (before the first tick: for 15 minutes since this half saw
-- the view so), or the session scan the ticks read is stale. Without one,
-- while another daemon ticks, a muted line says so. Without a view the empty
-- state tells the failure instead.
local function waitingView(b: Block, now: number, into: { Node })
	if not store.waiting and not store.waitingError then
		table.insert(into, ui.node("skeleton", { key = "list", shape = "rows", rows = 8, aria = "Reading waits" }))
		return
	end
	if store.waiting then
		local health = val(store.waiting.health)
		health = if type(health) == "table" then health else {}
		local checked = epoch(val(health.checked_at))
		local since = if checked then absoluteTime(checked, now) else "never"
		-- One line, capped as the applet and omp cap it.
		local err = string.match(oneLine(store.waitingError or val(health.error)), "^%s*(.-)%s*$")
		local scan = epoch(val(health.agents_stale_since))
		local last = checked or store.waitingUncheckedSince
		local text, tone = nil, "warning"
		if err ~= "" then
			text, tone = "⚠️ Waits not checked since " .. since .. ": " .. clip(err, 120), "error"
		elseif last and now - last > 15 * 60 then
			text = "⚠️ Waits not checked since " .. since
		elseif scan then
			text = "⚠️ Session scan stale since " .. absoluteTime(scan, now)
		elseif store.waitingSkipped then
			text, tone = "Another Tern daemon checks these waits", "muted"
		end
		if text then
			table.insert(into, withp(ui.text({ ui.span(text, tone) }), { key = "health", role = "codexbar.health" }))
		end
	end
	local q = string.lower(b.filter)
	local sel = selected(b)
	local kids: { Node } = {}
	for _, g in waitingGroups(q) do
		table.insert(kids, groupHead("w-" .. g.ball.id, { ui.span(g.ball.title, "strong"), ui.span("  " .. #g.rows, "muted") }))
		for _, r in g.rows do
			addWaitingRow(kids, r, now, q)
			local waitingFor = oneLine(r.waiting_for)
			if waitingFor ~= "" and waitingFor ~= waitTitle(r) then -- a pane record named by the text says it already
				local spans: { any } = { ui.span("Waiting for: ", "muted") }
				for _, s in highlighted(clip(waitingFor, 100), q) do
					table.insert(spans, s)
				end
				table.insert(kids, withp(ui.text(spans), { key = "wf-" .. waitKey(r), role = "codexbar.waitingfor" }))
			end
			if sel and sel.key == waitKey(r) then
				table.insert(kids, waitingPeek(b, r, now))
			end
		end
	end
	local empty = if b.filter ~= "" then noMatch(b, "wait")
		elseif not store.waiting then emptyNode("warn", "Waits unavailable", store.waitingError)
		else emptyNode("clock", "Nothing is set aside", "Ctrl+Alt+B sets the focused session aside.")
	table.insert(into, listView(b, kids, empty))
end

local function key(text: string): Span
	return ui.span(text, "key")
end

local function seg(k: string, text: string, props: { [string]: any }?, kids: { Node }?): Node
	return withp(ui.node("seg", { key = k, text = text }, kids), props or {})
end

local function dockView(b: Block, now: number): Node
	local segs: { Node } = {}
	-- A hint's label starts with a no-break space: a wrapped footer keeps each key beside its label.
	local hints: { any } = { key("←"), key("→"), ui.span("\u{a0}tabs   ", "dim") }
	if b.tab == "usage" then
		local snap = store.usage
		local at = math.max(store.usageAt, measuredAt(snap))
		if store.fetching and #setting("providers") > 0 then
			table.insert(segs, seg("fetch", "Refreshing…", { priority = 2 }, { ui.node("spinner", { style = "dots" }) }))
		elseif at > 0 then
			table.insert(segs, seg("at", "Updated " .. absoluteTime(at, now), { icon = "clock", priority = 2 }))
		end
		if store.usageError and store.usage then -- without a snapshot the error fills the page instead
			table.insert(segs, seg("err", store.usageError, { icon = "warn", tone = "error", priority = 3, min = { w = "20ch" } }))
		end
		if type(snap) == "table" and str(snap.cliVersion) ~= "" then
			table.insert(segs, seg("cli", "codexbar " .. str(snap.cliVersion), { side = "right", priority = 1 }))
		end
		for _, n in { key("r"), ui.span("\u{a0}refresh   ", "dim"), key("esc"), ui.span("\u{a0}close", "dim") } do
			table.insert(hints, n)
		end
	else
		local c = { working = 0, blocked = 0, idle = 0, untracked = 0 }
		for _, a in liveAgents() do
			c[str(a.state)] = (c[str(a.state)] or 0) + 1
		end
		if b.tab == "waiting" then
			local w = waitingCounts()
			local onMe = tonumber(val(w.on_me)) or 0
			table.insert(segs, seg("on_me", onMe .. " on me", { icon = "warn", tone = if onMe > 0 then "warning" else "muted", priority = 5 }))
			table.insert(segs, seg("machine", (tonumber(val(w.on_machine)) or 0) .. " watched", { icon = "activity", tone = "muted", priority = 4 }))
			table.insert(segs, seg("timer", (tonumber(val(w.on_timer)) or 0) .. " on a timer", { icon = "history", tone = "muted", priority = 3 }))
			table.insert(segs, seg("someone", (tonumber(val(w.on_someone)) or 0) .. " on something", { icon = "users", tone = "muted", priority = 3 }))
			table.insert(segs, seg("my_time", (tonumber(val(w.on_my_time)) or 0) .. " when I have time", { icon = "clock", tone = "muted", priority = 2 }))
		elseif b.tab == "agents" then
			table.insert(segs, seg("working", c.working .. " working", { icon = "activity", tone = if c.working > 0 then "success" else "muted", priority = 4 }))
			table.insert(segs, seg("blocked", c.blocked .. " blocked", { icon = "warn", tone = if c.blocked > 0 then "error" else "muted", priority = 5 }))
			table.insert(segs, seg("idle", c.idle .. " idle", { icon = "pause", tone = "muted", priority = 2 }))
			if c.untracked > 0 then
				table.insert(segs, seg("untracked", c.untracked .. " untracked", { icon = "help", tone = "info", priority = 1 }))
			end
		else
			local reboot, exited = historyRows("")
			table.insert(segs, seg("reboot", #reboot .. " to restore", { icon = "repeat", tone = if #reboot > 0 then "warning" else "muted", priority = 3 }))
			table.insert(segs, seg("exited", #exited .. " recently closed", { icon = "history", tone = "muted", priority = 2 }))
		end
		if store.scanning then
			table.insert(segs, seg("scan", "Scanning", { side = "right", priority = 1 }, { ui.node("spinner", { style = "dots" }) }))
		end
		for _, n in {
			key("↑"), key("↓"), ui.span("\u{a0}select   ", "dim"),
			key("⏎"), ui.span(if b.tab == "agents" then "\u{a0}focus   " elseif b.tab == "waiting" then "\u{a0}open   " else "\u{a0}launch   ", "dim"),
		} do
			table.insert(hints, n)
		end
		local row = if b.tab ~= "history" then selected(b) else nil
		if row and cardPane(row.record, row.waiting == true) then
			table.insert(hints, key("shift+enter"))
			table.insert(hints, ui.span("\u{a0}card   ", "dim"))
		end
		if b.filter == "" then
			table.insert(hints, key("space"))
			table.insert(hints, ui.span(if b.tab == "waiting" then "\u{a0}details   " else "\u{a0}conversation   ", "dim"))
		end
		if b.tab == "agents" then
			table.insert(hints, key("tab"))
			table.insert(hints, ui.span(if setting("hideOld") then "\u{a0}show\u{a0}old   " else "\u{a0}hide\u{a0}old   ", "dim"))
		elseif b.tab == "waiting" then
			if b.filter == "" then
				table.insert(hints, key("+"))
				table.insert(hints, ui.span("\u{a0}1\u{a0}day   ", "dim"))
			end
			table.insert(hints, key("del"))
			table.insert(hints, ui.span("\u{a0}done   ", "dim"))
			table.insert(hints, key("f2"))
			table.insert(hints, ui.span("\u{a0}waiting\u{a0}for   ", "dim"))
			if row and row.waiting and select(2, firstDue(row.record)) then
				table.insert(hints, key("f5"))
				table.insert(hints, ui.span("\u{a0}retry   ", "dim"))
			end
		elseif b.tab == "history" then
			table.insert(hints, key("del"))
			table.insert(hints, ui.span("\u{a0}dismiss   ", "dim"))
		end
		table.insert(hints, key("esc"))
		table.insert(hints, ui.span(if b.filter ~= "" then "\u{a0}clear" else "\u{a0}close", "dim"))
	end
	table.insert(segs, seg("settings", "Settings", { icon = "gear", side = "right", priority = 6, actions = { click = "settings" } }))
	return ui.col({
		ui.node("status", { key = "status", transparent = true }, segs),
		withp(ui.text(hints), { key = "keys" }),
	})
end

local function tabsNode(b: Block): Node
	local reboot, exited = historyRows("")
	local onMe = tonumber(val(waitingCounts().on_me)) or 0
	local counts = { agents = #liveAgents(), waiting = onMe, history = #reboot + #exited }
	local items = {}
	for _, id in TABS do
		local label: { any } = { ui.span(TAB_NAMES[id]) }
		if counts[id] then
			local alert = (id == "history" and #reboot > 0) or (id == "waiting" and onMe > 0)
			table.insert(label, ui.span(" " .. counts[id], if alert then "warning" else "muted"))
		end
		table.insert(items, { id = id, label = label })
	end
	return ui.node("tabs", { key = "tabs", items = items, active = b.tab })
end

-- The filter field; on Agents its attached eye hides or shows sessions older than a day.
local function filterNode(b: Block): Node
	local buttons = nil
	if b.tab == "agents" then
		local hide = setting("hideOld")
		local n = if hide then oldCount() else 0
		local tip = if hide then "Show " .. n .. " sessions older than a day (Tab)" else "Hide sessions older than a day (Tab)"
		buttons = { {
			act = "hide_old",
			icon = if hide then "eye-off" else "eye",
			text = if n > 0 then n .. " hidden" else nil,
			title = tip,
		} }
	end
	return ui.node("input", {
		key = "filter",
		text = b.filter,
		cursor = #b.filter,
		placeholder = if b.tab == "waiting" then "Type to filter by title, note, project or wait"
			else "Type to filter by title, prompt, folder, provider or model",
		icon = "search",
		clear = true,
		buttons = buttons,
	})
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

-- Layout work belongs to the window half; the dashboard's window takes this up
-- like `settingsRequest`, at once through the link, else on its poll. A target
-- gone from every window is said here, where every pane is known; `at` lets a
-- window that sees the request late drop it.
local function waitingRequest(b: Block, request: { [string]: any })
	if request.target then
		local live = false
		for _, p in tern.pane.list() do
			live = live or p.pane == request.target
		end
		if not live then
			b.cx:toast("info", "CodexBar AgentView: that pane has closed")
			return
		end
	end
	request.pane = b.cx.pane
	request.seq = tern.now()
	request.at = os.time()
	tern.kv.set("waitingRequest", request)
	b.cx:open("codexbar-agentview://request")
end

-- Switching to a session closes the dashboard, like picking from a popup.
-- `tern focus` reaches a pane in any window but refuses a parked one ("in no
-- tab"): then the window holding it unparks it into a tab of its own, as the
-- Waiting tab's Open does. codexbar_focus.py keeps Tern's message to itself:
-- exit 4 with `host=tern` is any failed `tern focus` (refusal, timeout, no
-- Tern), so only the window, which knows whether the pane is parked, tells the
-- refusal apart; it reports `error` for anything else. Other failures are
-- reported here.
local function focusRecord(b: Block, r: any)
	python("codexbar_focus.py", { "--tern", "codexbar-agentview://focus/" .. str(r.sessionId) }, 15000, function(res)
		local pane = tonumber(val(r.ternPane))
		local ternFailed = res.status == 4 and string.find(res.stderr, "host=tern", 1, true) ~= nil
		if ternFailed and str(r.host) == "tern" and pane then
			waitingRequest(b, { op = "open", target = pane, error = firstLine(res.stderr) })
			return
		end
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
	if b.launched[key] and os.clock() - b.launched[key] < 60 then
		return
	end
	b.launched[key] = os.clock()
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
	local now = os.clock()
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

-- `verb` aimed at the row's session, or at its pane for a non-omp one.
local function waitArgs(verb: string, r: any, extra: { string }?): { string }
	local id = str(r.session_id)
	local args = { verb, if id ~= "" then "--session" else "--pane", if id ~= "" then id else str(r.pane) }
	for _, a in extra or {} do
		table.insert(args, a)
	end
	return args
end

-- Resume, like History's Launch, holds a minute: until the agents scan lists the
-- session as live, a second press would start a second writer of its file.
local function waitingOpen(b: Block, r: any)
	local pane = tonumber(val(r.pane))
	if waitLive(r) and pane then
		waitingRequest(b, { op = "open", target = pane })
	elseif waitLive(r) then -- running outside Tern
		focusRecord(b, { sessionId = r.session_id })
	else
		local key = waitKey(r)
		if b.busy == key or (b.launched[key] and os.clock() - b.launched[key] < 60) then
			return
		end
		b.busy = key
		b.cx:render()
		waitingRun(b.cx, "resume", waitArgs("resume", r), function(data)
			b.busy = nil
			if data then
				b.launched[key] = os.clock()
				waitingRequest(b, { op = "launch", command = str(data.command), cwd = str(data.cwd), session = str(r.session_id) })
			end
			b.cx:render()
		end)
	end
end

local function waitingAct(b: Block, name: string, r: any)
	if name == "wait_open" then
		waitingOpen(b, r)
	elseif name == "wait_done" then
		waitingRun(b.cx, "done", waitArgs("done", r))
	elseif name == "wait_snooze" then
		local wait = firstDue(r)
		if wait then
			waitingRun(b.cx, "snooze", waitArgs("snooze", r, { "--wait", str(wait.id), "--for", "1d" }))
		end
	elseif name == "wait_retry" then
		local _, wait = firstDue(r)
		if wait then
			waitingRun(b.cx, "retry", waitArgs("retry", r, { "--wait", str(wait.id) }))
		end
	elseif name == "wait_accept" then
		waitingRun(b.cx, "accept note", waitArgs("note", r, { "--accept-suggested" }))
	elseif name == "wait_card" then
		waitingRequest(b, { op = "open", target = tonumber(val(r.pane)), card = true })
	elseif name == "wait_what" then
		-- `cx:prompt` is the window's: it asks over the dashboard and calls `what`.
		waitingRequest(b, { op = "what", args = waitArgs("what", r), text = oneLine(r.waiting_for) })
	end
end

local function activate(b: Block)
	local row = selected(b)
	if not row then
		return
	end
	if row.waiting then
		waitingOpen(b, row.record)
	elseif row.history then
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
	elseif name == "hide_old" then
		local raw = tern.kv.get("settings")
		local settings = if type(raw) == "table" then table.clone(raw) else {}
		settings.hideOld = not setting("hideOld")
		tern.kv.set("settings", settings)
		renderAll()
	elseif name == "restore_all" then
		restoreAll(b)
	elseif name == "dismiss_all" then
		dismiss(b, (historyRows(string.lower(b.filter))))
	elseif name == "peek" then
		b.peek = not b.peek
	elseif name == "clear" then -- the filter's × and the no-match Clear button
		-- The selected row stays selected, so Esc then Space peeks at the session the filter found.
		b.sel = if row then row.key else nil
		b.filter = ""
	elseif row and row.waiting then
		waitingAct(b, name, row.record)
	elseif row then
		if name == "focus" then
			focusRecord(b, row.record)
		elseif name == "launch" then
			launchRecord(b, row.record)
		elseif name == "card" then
			waitingRequest(b, { op = "open", target = tonumber(val(row.record.ternPane)), card = true })
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
				table.insert(main, ui.node("empty", { key = "state", icon = "gauge", title = "Usage providers are off", text = "Turn providers on in Settings." }, {
					button("Open settings", "settings", { size = "md", icon = "gear" }),
				}))
			elseif store.usage then
				for _, card in providerCards(store.usage, now) do
					table.insert(main, card)
				end
			elseif store.usageError then
				table.insert(main, ui.node("empty", { key = "state", tone = "error", title = "Usage unavailable", text = store.usageError }, {
					button("Retry", "refresh", { size = "md", icon = "repeat", keys = { "r" } }),
				}))
			else
				table.insert(main, ui.node("skeleton", { key = "state", shape = "rows", rows = 4, aria = "Reading usage" }))
			end
		else
			table.insert(main, filterNode(b))
			if b.tab == "waiting" then
				waitingView(b, now, main)
			elseif not store.agents then
				table.insert(main, ui.node("skeleton", { key = "list", shape = "rows", rows = 8, aria = "Scanning agents" }))
			elseif b.tab == "agents" then
				table.insert(main, agentsView(b, now * 1000))
			else
				table.insert(main, historyView(b, now * 1000))
			end
			-- Tern 0.9 lays the main region out at its content height, so a flexed list gets none
			-- of the pane; this box is absolutely sized to the region and gives the list the rest.
			main = { ui.el("div", { key = "fill", class = "cb-fill" }, main) }
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
				act(b, "clear")
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
		if name == "tab" and b.tab == "agents" then
			act(b, "hide_old")
			return true
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
		elseif name == "enter" and k.shift then
			-- The Card button's key (Tern reports Shift+Space as a plain Space). A row
			-- without the button, History's included, ignores it.
			local row = selected(b)
			if row and not row.history and cardPane(row.record, row.waiting == true) then
				act(b, if row.waiting then "wait_card" else "card")
			end
		elseif name == "space" and b.filter == "" then
			-- Space peeks only while no query is in progress; after that it is a filter character.
			b.peek = not b.peek
		elseif name == "enter" then
			activate(b)
		elseif name == "delete" and b.tab == "history" then
			act(b, "dismiss")
		elseif name == "delete" and b.tab == "waiting" then
			act(b, "wait_done")
		elseif k.text == "+" and b.tab == "waiting" and b.filter == "" then
			act(b, "wait_snooze")
		elseif name == "f2" and b.tab == "waiting" then
			act(b, "wait_what")
		elseif name == "f5" and b.tab == "waiting" then
			act(b, "wait_retry")
		elseif name == "backspace" then
			if b.filter == "" then
				return false
			end
			local cut = utf8.offset(b.filter, -1) or #b.filter
			b.filter = string.sub(b.filter, 1, cut - 1)
			b.sel = nil
		elseif name == "paste" or name == "space" or (k.text and k.text ~= "" and string.byte(k.text) >= 32) then
			b.filter ..= if name == "space" then " " else oneLine(k.text)
			-- A filter never starts with whitespace, so `b.filter == ""` is the one test for "no query".
			b.filter = (string.gsub(b.filter, "^%s+", ""))
			b.sel = nil
		else
			return false
		end
		return true
	end,

	event = function(b: Block, ev: UiEvent, cx: BlockCx)
		if ev.ev == "select" and type(ev.item) == "string" and table.find(TABS, ev.item) then
			b.tab = ev.item
			b.sel = nil
		elseif (ev.ev == "select" or ev.ev == "activate") and type(ev.item) == "string" then
			if string.sub(ev.item, 1, #LIST_PATH) == LIST_PATH then
				b.sel = string.sub(ev.item, #LIST_PATH + 1)
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
	providers = "Choose which providers appear in usage and the widget bar. Credentials stay in ~/.codexbar/config.json.",
	agents = "Choose which live agents and recently closed sessions appear.",
	general = "The plugin reads the applet's usage cache. It fetches only after two refresh intervals without a measurement, at most once per interval across Tern daemons and the applet. A live Claude fetch may start a probe session when rate-limited.",
	about = "CodexBar AgentView brings provider usage and coding-agent sessions to KDE Plasma and Tern. It uses the upstream CodexBar CLI for usage data.",
}
local SETTINGS_ROWS: { any } = {
	{ id = "forecast", page = "usage", section = "Details", label = "Codex reset forecast",
		hint = "Show the cached reset forecast. Enabling it does not fetch immediately." },
	{ id = "recentUsage", page = "usage", section = "Details", label = "Recent consumption",
		hint = "Show the last hour's 5h consumption and last day's weekly consumption." },
	{ id = "claudeComposite5h", page = "usage", section = "Several accounts", label = "Claude combined 5h window",
		hint = "Show a plan-weighted total across current Claude accounts." },
	{ id = "claudeComposite7d", page = "usage", section = "Several accounts", label = "Claude combined weekly window",
		hint = "Show a plan-weighted total across current Claude accounts." },
	{ id = "composite5h", page = "usage", section = "Several accounts", label = "Codex combined 5h window",
		hint = "Show a plan-weighted total across current Codex accounts." },
	{ id = "composite7d", page = "usage", section = "Several accounts", label = "Codex combined weekly window",
		hint = "Show a plan-weighted total across current Codex accounts." },
}
for _, provider in PROVIDER_ORDER do
	table.insert(SETTINGS_ROWS, {
		id = "provider." .. provider, provider = provider, page = "providers", section = "Usage providers",
		label = PROVIDER_NAMES[provider],
		hint = if provider == "typesafe" then "Balance only. Configure its cookie source in the CodexBar CLI credentials file."
			else "Use the shared provider cache and existing authentication.",
	})
end
for _, row in {
	{ id = "includeNonTern", page = "agents", section = "Session visibility", label = "Include non-Tern agents",
		hint = "Show agents from other recognized hosts in Agents and History on Linux and macOS. Focus requires a supported host route." },
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
		local opener = if MACOS then "/usr/bin/open" else "xdg-open"
		local ok, err = pcall(tern.process.run, { opener, row.hint }, { timeout_ms = 10000 }, function(res: ProcessResult)
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
store.lastPoke = tern.kv.get("waitingPoke")

-- Which daemon process this is, for `tick --daemon`: Tern's API names none, so it
-- is the process's pid and start time. A plugin reload keeps them, and so does an
-- update (the daemon becomes the new build in place, its programs running on); a
-- daemon restart changes them. Linux reads them in /proc, as the CLI does for the
-- daemon that runs a command; elsewhere `ps` reads them for a shell's parent, this
-- process. Without either, ticks go without `--daemon`.
-- The timed tick waits for the answer (`daemonKnown`). In a callback of its own:
-- the main chunk is at Luau's 200-local limit.
tern.timer(0, function()
	local ok, stat = pcall(tern.fs.read, "/proc/self/stat", 4096)
	local pid, rest
	if ok then
		pid, rest = string.match(stat, "^(%d+) %(.*%) (.*)$")
	end
	local fields = if rest then string.split(rest, " ") else {}
	if pid and fields[20] then -- the fields after the name start at the third; the start time is the 22nd
		store.daemonId, store.daemonKnown = pid .. "-" .. fields[20], true
	elseif tern.runtime.os ~= "linux" then -- on Linux, only /proc's form: a second one would read as another daemon
		local started = pcall(tern.process.run, { "/bin/sh", "-c", 'echo "$PPID" $(ps -o lstart= -p "$PPID")' }, { timeout_ms = 10000 }, function(res: ProcessResult)
			local id = (string.gsub(string.match(res.stdout, "^%s*(.-)%s*$") or "", "%s+", "-"))
			store.daemonId = if res.status == 0 and string.match(id, "^%d+%-%S") then id else nil
			store.daemonKnown = true
		end)
		if not started then -- else the callback says, whenever it runs
			store.daemonKnown = true
		end
	else
		store.daemonKnown = true
	end
end)
tern.timer(0, tick)

-- The set-aside items (waiting_menu.json, validated by the CLI), once per load
-- and again a minute after a failure: the window half builds its pane picker from them.
store.loadMenu = function()
	python(WAITING_CLI, { "menu" }, 30000, function(res)
		local okj, data = pcall(tern.json.decode, res.stdout)
		if res.status == 0 and okj and type(data) == "table" and val(data.ok) == true and type(val(data.items)) == "table" then
			tern.kv.set("waitingMenu", { items = val(data.items) })
			return
		end
		local detail = if okj and type(data) == "table" then str(data.error) else ""
		detail = if detail ~= "" then detail else firstLine(res.stderr)
		tern.kv.set("waitingMenu", { error = if detail ~= "" then detail else "exit " .. res.status })
		tern.log.warn("codexbar: waiting menu failed", detail)
		store.menuRetryAt = os.clock() + 60
	end)
end
store.loadMenu()
