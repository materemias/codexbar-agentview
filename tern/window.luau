-- Window half of the CodexBar AgentView plugin: status-line segments, palette commands,
-- and the pane you came from, which the dashboard marks with a ring.
--
-- The host half publishes `summary` (status-line data) and `settingsRequest`
-- (dashboard settings button) through `tern.kv`; this half answers with `goto`
-- (switch a dashboard tab) and `focus` (the last focused pane that is not a
-- CodexBar AgentView block).
--
-- For sessions set aside it adds the set-aside command, which on an omp pane asks
-- the omp extension to open its menu and elsewhere shows Tern's own picker, and
-- the session card, also opened from `codexbar-agentview://card/<id>` links. It
-- carries out `waitingActions`: one leader window shows the toasts, and each
-- pane's action falls to the window holding that pane, as windows don't share
-- panes. So do its agent and pane events and tab colors. `waitingPoke` makes the
-- host tick after a CLI call from here; `codexbar-agentview://request`, from the
-- host, makes this half take `waitingRequest` up without waiting for the poll.
--
-- `tern.kv` and `tern.process` belong to the machine each half runs on: a window
-- attached to a remote host runs this half on its own machine, beside another
-- daemon. Layout work therefore goes through the window API only.

local KIND = "codexbar-agentview.dashboard"
local SETTINGS_KIND = "codexbar-agentview.settings"
local CANVAS_KIND = "tern.canvas"
local SCRIPTS = tern.plugin.dir .. "/../contents/scripts"
local LEASE_S = 30
local CARD_ABOUT = "Session card of pane "

local function paneInfo(cx: WindowCx, pane: number): PaneInfo?
	for _, p in cx.session:panes() do
		if p.pane == pane then
			return p
		end
	end
	return nil
end

-- The dashboard this window opened, with the layout from before it opened.
type Popup = {
	pane: number, tab: number?, snap: LayoutSnapshot,
	deadline: number, restoreBy: number, restoring: boolean, placing: boolean,
	closed: boolean, equalized: boolean, moves: number, targets: { [number]: boolean },
}
local opened: Popup? = nil

local restoreWaiters: { (WindowCx) -> () } = {} -- see `afterRestore`

local function restoreFinished(cx: WindowCx)
	local waiters = restoreWaiters
	restoreWaiters = {}
	for _, fn in waiters do
		fn(cx)
	end
end

local function closePane(cx: WindowCx, pane: number)
	if opened and opened.pane == pane then
		opened.placing = false
		opened.closed = true
	end
	cx.layout:close(pane)
end

-- However the dashboard closes (toggle, Esc, switching to a session, focus
-- leaving it, closing the pane), the layout returns to how it was before it
-- opened. `pane_closed` fires before the tree drops the pane, `tern close`
-- from the CLI sends none, and a pane can leave `session:panes()` while its
-- tab tree still holds it. So the restore waits for both, looking on each
-- frame as what it holds up (`afterRestore`) is a card or a focus to show. It
-- refuses when other blocks opened or closed meanwhile, which no retry changes:
-- the layout then stays as it is.
local function inTrees(cx: WindowCx, pane: number): boolean
	local function walk(n: any): boolean
		if type(n) ~= "table" then
			return false
		end
		local p = n.pane
		if type(p) == "number" and p == pane then
			return true
		end
		return walk(n[1]) or walk(n[2])
	end
	for _, t in cx.session:tabs() do
		local layout = cx.session:layout(t.id)
		if layout and walk(layout.root) then
			return true
		end
	end
	return false
end

local function attemptRestore(cx: WindowCx, o: Popup)
	if opened ~= o then
		return
	end
	local gone = not paneInfo(cx, o.pane) and not inTrees(cx, o.pane)
	if not gone and tern.now() < o.restoreBy then
		tern.timer(16, function(cx2)
			if cx2 then
				attemptRestore(cx2, o)
			end
		end)
		return
	end
	opened = nil
	local ok, err = false, "the dashboard is still in the layout"
	if gone then
		ok, err = cx.layout:restore(o.snap)
	end
	if not ok then
		tern.log.info("codexbar: layout not restored", err)
	end
	restoreFinished(cx)
end

local function restoreLayout(cx: WindowCx)
	local o = opened
	if not o or o.restoring or (paneInfo(cx, o.pane) and not o.closed) then
		return
	end
	if o.placing and o.tab == nil and tern.now() < o.deadline then
		return
	end
	o.restoring = true
	o.placing = false
	o.restoreBy = tern.now() + 2000
	attemptRestore(cx, o)
end

-- Closes the dashboard this window opened and runs `fn` once its layout is back:
-- the restore refuses after a block opened and would park an unparked pane
-- again. A close has usually landed by now, so this restores here and then, with
-- no timer in the way: one can take a few hundred ms to fire in an idle window.
local function afterRestore(cx: WindowCx, fn: (WindowCx) -> ())
	local o = opened
	if not o then
		fn(cx)
		return
	end
	table.insert(restoreWaiters, fn)
	if not o.closed then
		closePane(cx, o.pane)
	end
	restoreLayout(cx)
end

-- The dashboard to act on in this window (the focused one, else the first) and
-- the tab it shows (from its title).
local function dashboard(cx: WindowCx): (PaneInfo?, string?)
	local found: PaneInfo? = nil
	for _, p in cx.session:panes() do
		if p.block == KIND and (not found or p.focused) then
			found = p
		end
	end
	if not found then
		return nil, nil
	end
	local title = string.lower(found.title or "")
	return found, string.match(title, "(%a+)$")
end

type Placement = { target: number?, rightmost: boolean, tiles: number }

-- Split ratios give each tiled pane's normalized left/top bounds; nil while the
-- pane is not in the tab's tree yet.
local function placement(cx: WindowCx, pane: number, tab: number): Placement?
	local layout = cx.session:layout(tab)
	if not layout then
		return nil
	end
	local popupX, target = nil, nil
	local targetX, targetY = -1, -1
	local epsilon = 1e-6
	local tiles = 0
	local function walk(n: TabTree, x: number, y: number, w: number, h: number)
		if n.pane ~= nil then
			tiles += 1
			if n.pane == pane then
				popupX = x
			elseif x > targetX + epsilon or (math.abs(x - targetX) <= epsilon and y > targetY) then
				target, targetX, targetY = n.pane, x, y
			end
		elseif n[1] and n[2] then
			local ratio = n.ratio or 0.5
			if n.split == "right" then
				walk(n[1], x, y, w * ratio, h)
				walk(n[2], x + w * ratio, y, w * (1 - ratio), h)
			else
				walk(n[1], x, y, w, h * ratio)
				walk(n[2], x, y + h * ratio, w, h * (1 - ratio))
			end
		end
	end
	walk(layout.root, 0, 0, 1, 1)
	if popupX == nil then
		return nil
	end
	return { target = target, rightmost = popupX >= targetX - epsilon, tiles = tiles }
end

local function placementActive(cx: WindowCx, o: Popup): boolean
	if opened ~= o or not o.placing or o.restoring then
		return false
	end
	local focused = cx.session:focused()
	local info = paneInfo(cx, o.pane)
	if focused ~= o.pane
		or (info and (info.block ~= KIND or (o.tab ~= nil and info.tab ~= o.tab)))
		or (not info and o.tab ~= nil) then
		o.placing = false
		return false
	end
	if info and o.tab == nil then
		o.tab = info.tab
	end
	return true
end

local function placementFailed(cx: WindowCx, o: Popup, err: string)
	tern.log.warn("codexbar: cannot place dashboard", err)
	cx:toast("error", "Could not place CodexBar AgentView dashboard", err)
	closePane(cx, o.pane)
end

-- Moves the dashboard right of the rightmost tile until it is the rightmost
-- itself, then equalizes the tab. Layout changes land on a later tick, so each
-- step re-reads the tree on the next timer.
local function placeDashboard(cx: WindowCx, o: Popup)
	if not placementActive(cx, o) then
		return
	end
	if tern.now() >= o.deadline then
		placementFailed(cx, o, "Timed out placing the CodexBar AgentView dashboard")
		return
	end
	local function again()
		tern.timer(50, function(cx2)
			if cx2 then
				placeDashboard(cx2, o)
			end
		end)
	end
	local tab = o.tab
	local layout = if tab then placement(cx, o.pane, tab) else nil
	if not tab or not layout then
		again()
		return
	end
	if layout.rightmost then
		-- Equalizing can change which stacked row has the rightmost leaf.
		if o.equalized then
			o.placing = false
		else
			cx:action("equalize")
			o.equalized = true
			again()
		end
		return
	end
	local target = layout.target :: number
	-- Moving only this leaf preserves the rest of the tree. A repeated target
	-- cannot progress; new targets are bounded by the tile count.
	if o.targets[target] or o.moves >= layout.tiles - 1 then
		placementFailed(cx, o, "CodexBar AgentView dashboard placement did not progress")
		return
	end
	o.targets[target] = true
	o.moves += 1
	if not cx.layout:move(o.pane, target, "right") then
		placementFailed(cx, o, "Could not move the CodexBar AgentView dashboard")
		return
	end
	o.equalized = false
	again()
end

-- Opens the dashboard on `tab`; on the shown, focused tab it closes it, like a popup.
local function toggle(cx: WindowCx, tab: string)
	local info, shown = dashboard(cx)
	if not info then
		-- A dashboard closing right now never restores: nothing waits for it.
		if opened then
			opened = nil
			restoreFinished(cx)
		end
		-- Capture the original layout before creating the dashboard.
		local snap = cx.layout:snapshot()
		local pane = cx:new_block(KIND, { tab }, "beside", { focus = true })
		if pane then
			local o: Popup = {
				pane = pane, snap = snap, deadline = tern.now() + 3000,
				restoreBy = 0, restoring = false, placing = true, closed = false,
				equalized = false, moves = 0, targets = {},
			}
			opened = o
			tern.timer(0, function(cx2)
				if cx2 then
					placeDashboard(cx2, o)
				end
			end)
		end
		return
	end
	if info.focused and shown == tab then
		closePane(cx, info.pane)
		return
	end
	tern.kv.set("goto", { pane = info.pane, tab = tab, seq = tern.now() })
	if not info.focused then
		cx.layout:focus(info.pane)
	end
end

-- Settings are a persistent tab, not a dashboard sidebar.
local function openSettings(cx: WindowCx)
	for _, p in cx.session:panes() do
		if p.block == SETTINGS_KIND then
			cx.layout:focus(p.pane)
			return
		end
	end
	cx:new_block(SETTINGS_KIND, nil, "tab", { focus = true })
end

tern.command({ id = "agents", title = "CodexBar AgentView: Agents", icon = "users", keys = { "ctrl+alt+a" }, run = function(cx)
	toggle(cx, "agents")
end })
tern.command({ id = "waiting", title = "CodexBar AgentView: Waiting", icon = "clock", keys = { "ctrl+alt+w" }, run = function(cx)
	toggle(cx, "waiting")
end })
tern.command({ id = "usage", title = "CodexBar AgentView: Usage", icon = "gauge", keys = { "ctrl+alt+u" }, run = function(cx)
	toggle(cx, "usage")
end })
tern.command({ id = "history", title = "CodexBar AgentView: History", icon = "history", keys = { "ctrl+alt+h" }, run = function(cx)
	toggle(cx, "history")
end })
tern.command({ id = "settings", title = "CodexBar AgentView: Settings", icon = "sliders", run = function(cx)
	openSettings(cx)
end })

local OUTCOME_TONES = {
	fulfilled = { card = "success", status = "ok" },
	attention = { card = "warning", status = "warn" },
	overdue = { card = "warning", status = "warn" },
}

tern.chrome.status(function(pane)
	local s = tern.kv.get("summary")
	if type(s) ~= "table" then
		return nil
	end
	local segments = {}
	for _, u in (if type(s.usage) == "table" then s.usage else {}) do
		table.insert(segments, { text = u.text, icon = u.icon, tone = u.tone, command = "plugin.codexbar-agentview.usage" })
	end
	local w = if type(s.waiting) == "table" then s.waiting else {}
	local here = if type(w.panes) == "table" then w.panes[tostring(pane.pane)] else nil
	if type(here) == "table" and here.text ~= "" then
		local tones = OUTCOME_TONES[here.outcome]
		table.insert(segments, {
			text = here.text,
			tone = if tones then tones.status elseif here.on_me then "warn" else "muted",
			command = "plugin.codexbar-agentview.session_card",
		})
	end
	local c = if type(s.counts) == "table" then s.counts else {}
	local parts = {}
	if (c.blocked or 0) > 0 then
		table.insert(parts, c.blocked .. " blocked")
	end
	if (c.working or 0) > 0 then
		table.insert(parts, c.working .. " working")
	end
	if (c.idle or 0) > 0 then
		table.insert(parts, c.idle .. " idle")
	end
	if #parts > 0 then
		table.insert(segments, {
			text = table.concat(parts, " · "),
			icon = "users",
			tone = if (c.blocked or 0) > 0 then "error" elseif (c.working or 0) > 0 then "ok" else "muted",
			command = "plugin.codexbar-agentview.agents",
		})
	end
	if (w.on_me or 0) > 0 then
		table.insert(segments, {
			text = w.on_me .. " on me",
			icon = "clock",
			tone = "warn",
			command = "plugin.codexbar-agentview.waiting",
		})
	end
	if (tonumber(s.restore) or 0) > 0 then
		table.insert(segments, { text = s.restore .. " to restore", icon = "history", tone = "warn", command = "plugin.codexbar-agentview.history" })
	end
	return if #segments > 0 then segments else nil
end)

local function remember(cx: WindowCx, pane: number?)
	if not pane then
		return
	end
	local info = paneInfo(cx, pane)
	if info and info.block ~= KIND and info.block ~= SETTINGS_KIND and info.block ~= CANVAS_KIND and tern.kv.get("focus") ~= pane then
		tern.kv.set("focus", pane)
	end
end

-- Waiting: the CLI ---------------------------------------------------------------

local function firstLine(text: string): string
	return string.match(text, "[^\n]+") or ""
end

local function shellQuote(text: string): string
	return "'" .. string.gsub(text, "'", "'\\''") .. "'"
end

local function trim(text: string): string
	return string.match(text, "^%s*(.-)%s*$") or text
end

local function python(script: string, args: { string }, done: (ProcessResult, WindowCx?) -> ())
	local argv = { tern.kv.get("python") or "python3", SCRIPTS .. "/" .. script }
	for _, a in args do
		table.insert(argv, a)
	end
	local ok, err = pcall(tern.process.run, argv, { timeout_ms = 30000 }, done)
	if not ok then
		done({ status = -1, stdout = "", stderr = tostring(err), timed_out = false }, nil)
	end
end

-- One read-only CLI call; `done` gets the answer, or nil and the error.
local function waitingRead(args: { string }, done: (any, string?, WindowCx?) -> ())
	python("codexbar_waiting.py", args, function(res, cx)
		local okj, data = pcall(tern.json.decode, res.stdout)
		if res.status == 0 and okj and type(data) == "table" and data.ok == true then
			done(data, nil, cx)
		else
			local detail = if okj and type(data) == "table" and type(data.error) == "string" then data.error else firstLine(res.stderr)
			done(nil, if detail ~= "" then detail else "exit " .. res.status, cx)
		end
	end)
end

-- One CLI call that can change the state: the host ticks after it.
local function waitingCli(args: { string }, done: (any, string?, WindowCx?) -> ())
	waitingRead(args, function(data, err, cx)
		if data then
			tern.kv.set("waitingPoke", tern.now())
		end
		done(data, err, cx)
	end)
end

local function waitingFailed(what: string, err: string?, cx: WindowCx?)
	tern.log.warn("codexbar: " .. what .. " failed", err)
	if cx then
		cx:toast("error", "CodexBar AgentView: " .. what .. " failed", err)
	end
end

-- A CLI call whose answer nothing here reads.
local function waitingSend(what: string, args: { string })
	waitingCli(args, function(data, err, cx)
		if not data then
			waitingFailed(what, err, cx)
		end
	end)
end

-- What the host published per pane.
local function waitingEntries(): { [string]: any }
	local s = tern.kv.get("summary")
	local w = if type(s) == "table" then s.waiting else nil
	local panes = if type(w) == "table" then w.panes else nil
	return if type(panes) == "table" then panes else {}
end

-- One pane's entry, nil when no record watches it.
local function waitingEntry(pane: number): any
	local entry = waitingEntries()[tostring(pane)]
	return if type(entry) == "table" then entry else nil
end

-- A pane's full record in the view: a live session's in the pane, else the
-- pane record's; `paneOnly` asks for the pane record alone.
local function waitingRecord(pane: number, paneOnly: boolean?): any
	local view = tern.kv.get("waitingView")
	if type(view) ~= "table" then
		return nil
	end
	for _, field in (if paneOnly then { "panes" } else { "sessions", "panes" }) do
		for _, r in (if type(view[field]) == "table" then view[field] else {}) do
			if r.pane == pane and (field == "panes" or r.live == true) then
				return r
			end
		end
	end
	return nil
end

-- The omp session a pane runs.
local function paneSession(pane: number): string?
	local map = tern.kv.get("paneSessions")
	local e = if type(map) == "table" then map[tostring(pane)] else nil
	return if type(e) == "table" and e.provider == "omp" then e.session else nil
end

-- The omp extension the view knows in `pane`, whatever `paneSessions` says: a
-- new omp session has no session file yet and shows as untracked there.
local function paneExtension(pane: number): any
	local view = tern.kv.get("waitingView")
	local panes = if type(view) == "table" then view.extension_panes else nil
	local e = if type(panes) == "table" then panes[tostring(pane)] else nil
	return if type(e) == "table" then e else nil
end

local function targetArgs(verb: string, pane: number, session: string?, extra: { string }?): { string }
	local args = { verb, if session then "--session" else "--pane", if session then session else tostring(pane) }
	for _, a in extra or {} do
		table.insert(args, a)
	end
	return args
end

-- Waiting: the session card --------------------------------------------------------

local ui = tern.ui
-- Owner panes whose unseen session the card was opened for on focus; cleared once
-- the host publishes the pane as seen, so the card opens once per firing.
local seenAsked: { [number]: boolean } = {}
-- An open card's refresh state by canvas pane: what it last read and when, and
-- the branch, which only the full read (`card` without `--cached`) brings.
type CardLive = { sig: string, cachedAt: number, fullAt: number, busy: boolean, branch: any }
local cardLive: { [number]: CardLive } = {}

-- JSON null (`tern.json.null`, truthy) as nil, as in the host half.
local function val(x: any): any
	return if x == tern.json.null then nil else x
end
-- How often an open card reads local files anyway (times, agent state, last
-- exchange), and how often it also asks GitHub (the status cache's interval).
local CARD_LOCAL_S = 60
local CARD_FULL_S = 600

-- The canvases carry their owner pane, and the session they show (none for a
-- pane record), in `about`, so a plugin reload forgets nothing.
local function cardOwner(about: string?): number?
	return tonumber(string.match(about or "", "^" .. CARD_ABOUT .. "(%d+)"))
end

-- The CLI's session ids: alphanumerics, `.`, `_` and `-`; `about` goes on with ". ".
local function cardSession(about: string?): string?
	return string.match(about or "", " for session (%w[%w%._%-]*)%. ")
end

local function cardOf(cx: WindowCx, pane: number): number?
	for _, c in cx.canvas:list() do
		if c.owner == tern.plugin.id and cardOwner(c.about) == pane then
			return c.pane
		end
	end
	return nil
end

-- The view's entry for a card: its session's, or for a pane record its pane's,
-- as JSON, so a card can tell when what it shows has changed.
local function cardSig(session: string?, owner: number): string
	local view = tern.kv.get("waitingView")
	if type(view) ~= "table" then
		return ""
	end
	for _, field in { "sessions", "panes" } do
		for _, r in (if type(view[field]) == "table" then view[field] else {}) do
			if (session and r.session_id == session) or (not session and field == "panes" and r.pane == owner) then
				return tern.json.encode(r)
			end
		end
	end
	return ""
end

local function cardSection(head: string, rows: { Node }): Node
	local kids: { Node } = { ui.text({ ui.span(head, "muted") }) }
	for _, row in rows do
		table.insert(kids, row)
	end
	return ui.lines(kids)
end

local function clipped(text: string, n: number): string
	local flat = string.gsub(text, "%s+", " ")
	if (utf8.len(flat) or #flat) <= n then
		return flat
	end
	return string.sub(flat, 1, (utf8.offset(flat, n) or (n + 1)) - 1) .. "…"
end

-- Seconds since the epoch of an ISO 8601 time; without an offset it counts as UTC.
-- A copy of the host half's: the halves share no code.
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

-- Largest unit only: "7m", "23h", "4d".
local function age(secs: number): string
	local mins = math.max(0, secs) // 60
	return if mins < 60 then mins .. "m" elseif mins < 1440 then (mins // 60) .. "h" else (mins // 1440) .. "d"
end

-- "project · branch · model · state · started 2h ago · active 3m ago"
local function cardFacts(r: any, now: number): string
	local parts: { string } = {}
	local function add(text: any)
		if type(text) == "string" and text ~= "" then
			table.insert(parts, text)
		end
	end
	add(r.project)
	add(r.branch)
	if type(r.model) == "string" then
		add((string.gsub(string.match(r.model, "[^/]*$") or r.model, "^claude%-", "")))
	end
	add(val(r.agent_state) or val(r.agent_state_raw))
	local started, active = epoch(r.started_at), epoch(r.active_at)
	if started then
		add("started " .. age(now - started) .. " ago")
	end
	if active then
		add("active " .. age(now - active) .. " ago")
	end
	return table.concat(parts, " · ")
end

-- The Waiting tab's peek in the host half colors PR states the same.
local PR_STATE_TONE = { open = "success", draft = "muted", merged = "info", closed = "error" }
local PR_REVIEW_TONE = { approved = "success", changes_requested = "error", review_required = "muted" }
local PR_CHECKS_TONE = { passing = "success", failing = "error", pending = "warning" }

-- "✅ Fulfilled · PR#20 merged · 2h ago", "⚠️ Needs you · reason", "⏰ Overdue · label".
-- While other threads still wait, a fired one isn't "Fulfilled": the banner says
-- what came and, under it, what it still waits for.
local function cardBanner(r: any, now: number): Node?
	local tones = OUTCOME_TONES[r.outcome]
	if not tones then
		return nil
	end
	local wait: any = {}
	for _, w in (if type(r.waits) == "table" then r.waits else {}) do
		if w.outcome == r.outcome then
			wait = w
			break
		end
	end
	local open: { string } = {}
	for _, t in (if type(r.still_waiting) == "table" then r.still_waiting else {}) do
		table.insert(open, tostring(t))
	end
	local head, detail, tail = "⏰ Overdue", val(wait.label), nil
	if r.outcome == "fulfilled" then
		local at = epoch(wait.fired_at)
		-- "Done" would read as the button: the banner just says what came.
		head = if #open > 0 then "✅" else "✅ Fulfilled"
		detail, tail = val(wait.fired_text), if at then age(now - at) .. " ago" else nil
	elseif r.outcome == "attention" then
		local reason = val(wait.reason)
		head, detail = "⚠️ Needs you", if reason and reason ~= "" then reason else val(wait.label)
	end
	local sep = if head == "✅" then " " else " · "
	local text = head .. (if detail and detail ~= "" then sep .. clipped(tostring(detail), 120) else "") .. (if tail then " · " .. tail else "")
	local lines: { Node } = { ui.text({ ui.span(text, tones.card) }) }
	if #open > 0 then
		table.insert(lines, ui.text({ ui.span("Still waiting for ", "muted"), clipped(table.concat(open, "  ·  "), 160) }))
	end
	return ui.node("row", { role = "codexbar.banner", tone = tones.card }, { ui.lines(lines) })
end

-- Why a PR is on the card: the session waits on it, created it, or only mentioned it.
local PR_SOURCE_TEXT = { wait = "waiting on", created = "created" }

local function prRow(pr: any): Node
	local text = "PR#" .. tostring(pr.number) .. " " .. clipped(tostring(val(pr.title) or val(pr.repo) or ""), 60)
	local kids: { Node } = { ui.text({ ui.link(text, tostring(val(pr.url) or ""), if pr.source == "mentioned" then "muted" else nil) }) }
	if PR_SOURCE_TEXT[pr.source] then
		table.insert(kids, ui.badge(PR_SOURCE_TEXT[pr.source], "muted"))
	end
	if PR_STATE_TONE[pr.state] then
		table.insert(kids, ui.badge(pr.state, PR_STATE_TONE[pr.state]))
	end
	if PR_REVIEW_TONE[pr.review] then
		table.insert(kids, ui.badge((string.gsub(pr.review, "_", " ")), PR_REVIEW_TONE[pr.review]))
	end
	if PR_CHECKS_TONE[pr.checks] then
		table.insert(kids, ui.badge("checks " .. pr.checks, PR_CHECKS_TONE[pr.checks]))
	end
	return ui.node("row", { wrap = true, gap = "sm", role = "codexbar.pr" }, kids)
end

-- "16:00" today, else "Nov 6, 16:00"; the Waiting tab in the host half writes times the same.
local MONTHS = { "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec" }
local function clockTime(t: number, now: number): string
	local hhmm = os.date("%H:%M", t)
	if os.date("%Y-%m-%d", t) == os.date("%Y-%m-%d", now) then
		return hhmm
	end
	local d = os.date("*t", t)
	return MONTHS[d.month] .. " " .. d.day .. ", " .. hhmm
end

-- Each wait with its mark; a fulfilled one in green. A chain's later steps are
-- muted: nothing watches them yet.
local function waitRow(w: any, now: number): Node
	local text = tostring(val(w.summary) or val(w.label) or w.kind)
	local spans: { Span | string } = {}
	if w.state == "pending" then
		table.insert(spans, ui.span("Then: " .. text, "muted"))
	else
		table.insert(spans, ui.span(tostring(val(w.mark) or "") .. text, if w.outcome == "fulfilled" then "success" else nil))
	end
	if type(w.progress) == "string" and w.progress ~= "" then
		table.insert(spans, ui.span("  " .. w.progress, "info")) -- the mark is 🔧 already
	end
	local due = epoch(w.due_at)
	if w.overdue == true then
		table.insert(spans, ui.span("  overdue", "error"))
	elseif due and due <= now then
		table.insert(spans, ui.span("  due", "warning"))
	elseif due then
		table.insert(spans, ui.span("  due in " .. age(due - now), "muted"))
	end
	return ui.text(spans)
end

-- Exact, unlike `age`: "90m", "2h", "45s".
local function period(secs: number): string
	if secs % 86400 == 0 then
		return (secs // 86400) .. "d"
	elseif secs % 3600 == 0 then
		return (secs // 3600) .. "h"
	elseif secs % 60 == 0 then
		return (secs // 60) .. "m"
	end
	return secs .. "s"
end

local function scheduleRow(j: any, now: number): Node
	local every = tonumber(j.every_s)
	local due = epoch(j.due_at)
	local text = if every then "every " .. period(every) else "once"
	if due then
		text ..= if due > now then " · next in " .. age(due - now) else " · next now"
	end
	return ui.text({ ui.span(text .. "  ", "muted"), clipped(tostring(j.prompt), 80) })
end

-- The recap with its time, else the last exchange, which is left for the full card. A recap
-- written before the session's later turns is shown dimmed, as an earlier one.
local function cardRecap(cx: WindowCx, pane: number, r: any, reading: boolean): Node?
	if type(r.recap) == "string" and r.recap ~= "" then
		local at = epoch(r.recap_at)
		local head = (if r.recap_stale == true then "Earlier recap" else "Recap") .. (if at then " · " .. clockTime(at, os.time()) else "")
		return cardSection(head, { ui.text({ ui.span(clipped(r.recap, 300), if r.recap_stale == true then "muted" else nil) }) })
	end
	if reading then
		return nil
	end
	local ok, messages = pcall(function()
		return cx.agents:transcript(pane, { last = 2 })
	end)
	local lines: { Node } = {}
	for _, m in (if ok then messages else {}) do
		table.insert(lines, ui.text((if m.role == "user" then "You: " else "Agent: ") .. clipped(m.text, 200)))
	end
	if #lines == 0 and type(r.last_prompt) == "string" and r.last_prompt ~= "" then
		table.insert(lines, ui.text("You: " .. clipped(r.last_prompt, 200)))
	end
	return if #lines > 0 then cardSection("Last exchange", lines) else nil
end

-- `r` is a card, or while `reading` one comes, what the view knows.
local function cardView(cx: WindowCx, pane: number, r: any, reading: boolean): Node
	local now = os.time()
	local kids: { Node } = {}
	local banner = cardBanner(r, now)
	if banner then
		table.insert(kids, banner)
	end
	local function section(head: string, rows: { Node })
		if #rows > 0 then
			table.insert(kids, cardSection(head, rows))
		end
	end
	local function list(field: string): { any }
		return if type(r[field]) == "table" then r[field] else {}
	end
	if type(r.note) == "string" and r.note ~= "" then
		table.insert(kids, ui.text({ ui.span(r.note, "strong") }))
	end
	if type(r.suggested_note) == "string" and r.suggested_note ~= "" then
		table.insert(kids, ui.text({ ui.span("Suggested: ", "muted"), r.suggested_note }))
	end
	local aside = epoch(r.set_aside_at)
	if aside then
		table.insert(kids, ui.text({ ui.span("Set aside " .. age(now - aside) .. " ago", "muted") }))
	end
	local facts = cardFacts(r, now)
	if facts ~= "" then
		table.insert(kids, ui.text({ ui.span(facts, "muted") }))
	end
	local prs: { Node } = {}
	local mentioned: { Node } = {}
	for _, pr in list("prs") do
		table.insert(if pr.source == "mentioned" then mentioned else prs, prRow(pr))
	end
	section("Pull requests", prs)
	section("Mentioned", mentioned)
	local waits: { Node } = {}
	for _, w in list("waits") do
		table.insert(waits, waitRow(w, now))
	end
	section("Waiting for", waits)
	local schedules: { Node } = {}
	for _, j in list("schedules") do
		table.insert(schedules, scheduleRow(j, now))
	end
	section("Schedules", schedules)
	local since: { Node } = {}
	local history = list("history")
	for i = math.max(1, #history - 4), #history do
		local at = epoch(history[i].at)
		local text = clipped(tostring(history[i].text), 120)
		table.insert(since, ui.text({
			ui.span(if at then clockTime(at, now) .. "  " else "", "muted"),
			ui.span(text, if history[i].kind == "fired" then "success" else nil),
		}))
	end
	section("Since set aside", since)
	if type(r.next_step) == "string" and r.next_step ~= "" then
		section("Next step", { ui.text(r.next_step) })
	end
	local recap = cardRecap(cx, pane, r, reading)
	if recap then
		table.insert(kids, recap)
	end
	if reading then
		table.insert(kids, ui.text({ ui.span("Reading the session…", "dim") }))
	end
	return ui.col(kids)
end

local function cardTitle(cx: WindowCx, pane: number, r: any): string
	local info = paneInfo(cx, pane)
	local title = val(r.title) or val(r.label) or (info and info.title)
	return clipped(if type(title) == "string" and title ~= "" then title else "Session", 70)
end

-- Done and +1 day need waits of a record, and +1 day one that snoozing moves.
-- Both act on the pane's entry, so it has to be the card's session: a link can
-- name one the agents scan has not mapped to the pane yet.
local function cardDock(pane: number, r: any): Node
	local function act(text: string, name: string, variant: string?): Node
		return ui.button(text, { key = name, size = "sm", variant = variant, actions = { click = name } })
	end
	local kids: { Node } = {}
	local entry = waitingEntry(pane)
	if entry and entry.session == r.session_id and r.has_record ~= false and type(r.waits) == "table" and #r.waits > 0 then
		table.insert(kids, act("Done", "card_done", "primary"))
		if entry.wait then
			table.insert(kids, act("+1 day", "card_snooze"))
		end
	end
	table.insert(kids, act("Close", "card_close", "quiet"))
	return ui.row(kids)
end

-- Draws `card` into the open card `canvas` of `pane`.
local function showCard(cx: WindowCx, canvas: number, pane: number, card: any, reading: boolean)
	cx.canvas:set(canvas, {
		title = cardTitle(cx, pane, card),
		view = cardView(cx, pane, card, reading),
		dock = cardDock(pane, card),
	})
end

-- Floats the card over `pane`, one per pane, and marks the record seen. An omp
-- session needs no record: the card opens at once on what the view knows, a
-- skeleton without one, fills in from `card --cached`, local files only, and
-- then from `card`, which asks GitHub and can take seconds.
-- `known` is the session a link named: it wins over what the agents scan, which
-- lags a session started or resumed in the pane, maps there.
local function openCard(cx: WindowCx, pane: number, known: string?)
	if cardOf(cx, pane) then
		return
	end
	local record = waitingRecord(pane)
	if known and record and record.session_id ~= known then
		record = nil
	end
	-- A pane record stays what the pane shows: a session started there later has none of its waits.
	local session = if record then record.session_id else (known or paneSession(pane))
	if not record and not session then
		cx:toast("info", "CodexBar AgentView: this pane has no session card")
		return
	end
	local shown = record or {}
	local canvas, err = cx.canvas:open({
		title = cardTitle(cx, pane, shown),
		view = cardView(cx, pane, shown, session ~= nil),
		dock = cardDock(pane, shown),
		about = CARD_ABOUT .. pane .. (if session then " for session " .. session else "")
			.. ". Done, when shown, ends its waits, +1 day, when shown, snoozes the first due one, Close only closes this card.",
		how = "beside",
		target = pane,
		focus = false,
	})
	if not canvas then
		waitingFailed("session card", err, cx)
		return
	end
	local ok, floatErr = cx.layout:float(canvas, pane, "tr", { focus = false, size = { 0.45, 0.75 } })
	if not ok then
		cx.layout:close(canvas)
		waitingFailed("session card", floatErr, cx)
		return
	end
	if record and session and record.unseen == true then
		waitingSend("mark seen", { "seen", "--session", session })
	end
	if not session then
		return
	end
	local best = record -- what the card shows if the last read fails
	local function show(cx2: WindowCx, card: any, reading: boolean)
		best = card
		showCard(cx2, canvas, pane, card, reading)
	end
	-- What opened it counts as the first refresh of both kinds.
	cardLive[canvas] = { sig = cardSig(session, pane), cachedAt = os.time(), fullAt = os.time(), busy = false }
	local function closed(cx2: WindowCx?): boolean
		return not cx2 or cardOf(cx2, pane) ~= canvas
	end
	-- `card` can answer first: its success outdates `card --cached`, and its
	-- failure leaves the card to whatever `--cached` still brings.
	local cachedPending, fresh, failed = true, false, false
	local function settle(cx2: WindowCx)
		if best then
			show(cx2, best, false)
		else
			cx2.layout:close(canvas)
		end
	end
	waitingRead({ "card", "--session", session, "--cached" }, function(cached, _err, cx2)
		cachedPending = false
		if fresh or closed(cx2) then
			return
		end
		if cached then
			show(cx2 :: WindowCx, cached.card, not failed)
		elseif failed then
			settle(cx2 :: WindowCx)
		end
	end)
	-- `card` writes no state, so no host tick follows it.
	waitingRead({ "card", "--session", session }, function(data, readErr, cx2)
		fresh = data ~= nil
		if closed(cx2) then
			return
		end
		local cx3 = cx2 :: WindowCx
		if data then
			local live = cardLive[canvas]
			if live then
				live.branch = val(data.card.branch)
			end
			show(cx3, data.card, false)
			return
		end
		waitingFailed("session card", readErr, cx3)
		failed = true
		if not cachedPending then
			settle(cx3)
		end
	end)
end

-- Rereads each open card: from local files when its view entry changed or a
-- minute passed, and with GitHub every ten minutes; the branch of the last full
-- read stays in between. A pane record's card has no session to read and is
-- drawn again from the view, and closes with the record. Cards opened before a
-- plugin reload are taken over as if just read.
local function refreshCards(cx: WindowCx)
	local now = os.time()
	local open: { [number]: boolean } = {}
	for _, c in cx.canvas:list() do
		local owner = if c.owner == tern.plugin.id then cardOwner(c.about) else nil
		if not owner then
			continue
		end
		local canvas = c.pane
		open[canvas] = true
		local session = cardSession(c.about)
		local sig = cardSig(session, owner)
		local live = cardLive[canvas]
		if not live then
			cardLive[canvas] = { sig = sig, cachedAt = now, fullAt = now, busy = false }
			continue
		end
		if live.busy then
			continue
		end
		local full = session ~= nil and now - live.fullAt >= CARD_FULL_S
		if sig == live.sig and not full and now - live.cachedAt < CARD_LOCAL_S then
			continue
		end
		live.sig = sig
		live.cachedAt = now
		if not session then
			local record = waitingRecord(owner, true)
			if record then
				showCard(cx, canvas, owner, record, false)
			elseif type(tern.kv.get("waitingView")) == "table" then
				cx.layout:close(canvas)
			end
			continue
		end
		local args = { "card", "--session", session }
		if full then
			live.fullAt = now
		else
			table.insert(args, "--cached")
		end
		live.busy = true
		waitingRead(args, function(data, err, cx2)
			live.busy = false
			if not cx2 or cardOf(cx2, owner) ~= canvas then
				return
			end
			if data then
				if full then
					live.branch = val(data.card.branch)
				elseif val(data.card.branch) == nil then
					data.card.branch = live.branch
				end
				showCard(cx2 :: WindowCx, canvas, owner, data.card, false)
			else
				tern.log.warn("codexbar: session card refresh failed", err)
			end
		end)
	end
	for canvas in cardLive do
		if not open[canvas] then
			cardLive[canvas] = nil
		end
	end
end

-- The focused pane; from the dashboard or a card, the pane focus came from.
local function focusedTarget(cx: WindowCx): (number?, PaneInfo?)
	local pane = cx.session:focused()
	local info = if pane then paneInfo(cx, pane) else nil
	if info and (info.block == KIND or info.block == SETTINGS_KIND or info.block == CANVAS_KIND) then
		pane = tonumber(tern.kv.get("focus"))
		info = if pane then paneInfo(cx, pane) else nil
	end
	if pane and info then
		return pane, info
	end
	return nil, nil
end

-- Closes `pane`'s card, or opens it once a dashboard this closes has restored its layout.
local function toggleCardOn(cx: WindowCx, pane: number, known: string?)
	local canvas = cardOf(cx, pane)
	if canvas then
		cx.layout:close(canvas)
		return
	end
	local open = dashboard(cx)
	if open then
		closePane(cx, open.pane)
	end
	afterRestore(cx, function(cx2)
		openCard(cx2, pane, known)
	end)
end

local function toggleCard(cx: WindowCx)
	local pane = focusedTarget(cx)
	if not pane then
		cx:toast("info", "CodexBar AgentView: no pane to show a card for")
		return
	end
	toggleCardOn(cx, pane)
end

-- Tern's preset takes Ctrl+Alt+Shift+B (browser PiP); the omp extension's details panel shares this chord outside Tern.
tern.command({ id = "session_card", title = "CodexBar AgentView: Session card", icon = "message", keys = { "ctrl+alt+i" }, run = toggleCard })

tern.on("canvas_action", function(ev: CanvasAction, cx: WindowCx): boolean?
	local pane = cardOwner(ev.about)
	if not pane then
		return nil
	end
	-- The pane may run another session by now: act only on the one the card shows.
	local entry = waitingEntry(pane)
	if entry and entry.session ~= cardSession(ev.about) then
		entry = nil
	end
	if entry and ev.act == "card_done" then
		waitingSend("done", targetArgs("done", pane, entry.session))
	elseif entry and ev.act == "card_snooze" and entry.wait then
		waitingSend("snooze", targetArgs("snooze", pane, entry.session, { "--wait", entry.wait, "--for", "1d" }))
	end
	cx.layout:close(ev.pane)
	return true
end)

-- A float whose owner parked or closed moves over another pane; a card belongs to its owner.
local function dropOrphanCards(cx: WindowCx)
	for _, c in cx.canvas:list() do
		local owner = if c.owner == tern.plugin.id then cardOwner(c.about) else nil
		if owner then
			local info = paneInfo(cx, owner)
			if not info or info.parked then
				cx.layout:close(c.pane)
			end
		end
	end
end

-- Waiting: the leader window ---------------------------------------------------------

-- Holds the lease in `tern.kv`. A claim acts from the next poll on, once a
-- window that claimed at the same moment has overwritten it.
local function leading(cx: WindowCx): boolean
	local key = cx:window_key()
	local lease = tern.kv.get("waitingLeader")
	local now = os.time()
	local held = type(lease) == "table" and type(lease.expires) == "number"
	local mine = held and lease.key == key
	if not held or lease.expires <= now or (mine and lease.expires - now < LEASE_S - 10) then
		tern.kv.set("waitingLeader", { key = key, expires = now + LEASE_S })
	end
	return mine
end

-- `retryAt` and `tries` space out a failing ack: 5 s, doubling up to a minute.
type Acted = { result: string, error: string?, sending: boolean, acked: boolean, retryAt: number, tries: number }
local acted: { [string]: Acted } = {} -- by action id: the outbox repeats an action until its ack lands

local function carryOut(cx: WindowCx, a: any, info: PaneInfo?): (string, string?)
	if a.type == "toast" then
		local level = if a.level == "success" or a.level == "error" then a.level else "info"
		cx:toast(level, tostring(a.text), if type(a.sub) == "string" then a.sub else nil)
		return "done"
	end
	if not info then
		return "done" -- the session has no Tern pane
	end
	if a.type == "park" then
		if info.parked then
			return "done"
		end
		local ok, err = cx.layout:park(info.pane)
		return if ok then "done" else "failed", err
	elseif a.type == "close_pane" then
		cx.layout:close(info.pane)
		return "done"
	end
	return "failed", "unknown action " .. tostring(a.type)
end

-- Whether this window should take `a` up now. A pane action no window holds
-- stays queued for the window that will; the host acks it once the pane is gone.
local function ownsAction(a: any, info: PaneInfo?, leader: boolean): boolean
	if info then
		-- A dashboard's layout restore would bring a pane parked now back.
		return not (a.type == "park" and opened)
	end
	return leader and (a.type == "toast" or tonumber(a.pane) == nil)
end

local function runActions(cx: WindowCx, leader: boolean)
	local actions = tern.kv.get("waitingActions")
	local listed: { [string]: boolean } = {}
	for _, a in (if type(actions) == "table" then actions else {}) do
		local id = tostring(a.id)
		listed[id] = true
		local state = acted[id]
		local pane = if a.type ~= "toast" then tonumber(a.pane) else nil
		local info = if pane then paneInfo(cx, pane) else nil
		if not state and ownsAction(a, info, leader) then
			local ok, result, err = pcall(carryOut, cx, a, info)
			state = {
				result = if ok then result else "failed",
				error = if ok then err else tostring(result),
				sending = false,
				acked = false,
				retryAt = 0,
				tries = 0,
			}
			acted[id] = state
		end
		if state and not state.sending and not state.acked and os.time() >= state.retryAt then
			local sent = state
			sent.sending = true
			local args = { "ack", "--action", id, "--result", state.result }
			if state.error then
				table.insert(args, "--error=" .. state.error)
			end
			waitingCli(args, function(data, err)
				sent.sending = false
				sent.acked = data ~= nil
				if not data then
					sent.tries += 1
					sent.retryAt = os.time() + math.min(60, 5 * 2 ^ (sent.tries - 1))
					tern.log.warn("codexbar: ack failed", id, err)
				end
			end)
		end
	end
	-- An action gone from the outbox needs nothing more: acked here, by the host, or expired.
	for id, state in (if type(actions) == "table" then acted else {}) do
		if not listed[id] and not state.sending then
			acted[id] = nil
		end
	end
end

-- This window's tabs holding `on_me` panes turn green when all of those are
-- fulfilled, else yellow. Tabs the user colored stay theirs; the ones colored
-- here are kept per window in `waitingTabs:<key>` and cleared here.
local function colorTabs(cx: WindowCx)
	local tabsKey = "waitingTabs:" .. cx:window_key()
	local entries = waitingEntries()
	local want: { [number]: TabColor } = {}
	for _, p in cx.session:panes() do
		local entry = entries[tostring(p.pane)]
		if type(entry) == "table" and entry.on_me and p.tab then
			if entry.outcome ~= "fulfilled" then
				want[p.tab] = "yellow"
			elseif not want[p.tab] then
				want[p.tab] = "green"
			end
		end
	end
	local stored = tern.kv.get(tabsKey)
	local mine: { [number]: boolean } = {}
	for _, id in (if type(stored) == "table" then stored else {}) do
		mine[id] = true
	end
	if next(want) == nil and next(mine) == nil then
		return
	end
	local keep = tern.json.array()
	for _, t in cx.session:tabs() do
		local color = if t.color == tern.json.null then nil else t.color
		local ours = mine[t.id] and (color == "yellow" or color == "green")
		local wanted = want[t.id]
		if wanted and (color == nil or ours) then
			if color ~= wanted then
				cx.layout:color_tab(t.id, wanted)
			end
			table.insert(keep, t.id)
		elseif ours then
			cx.layout:color_tab(t.id, nil)
		end
	end
	table.sort(keep)
	if tern.json.encode(keep) ~= tern.json.encode(if type(stored) == "table" then stored else tern.json.array()) then
		tern.kv.set(tabsKey, keep)
	end
end

local function lead(cx: WindowCx)
	runActions(cx, leading(cx))
	colorTabs(cx)
end

tern.on("agent_state", function(ev: AgentStateEvent, _cx: WindowCx)
	local entry = if ev.new == "waiting_input" then waitingEntry(ev.pane) else nil
	if entry and entry.session then
		waitingSend("agent event", { "event", "--type", "waiting_input", "--session", entry.session })
	end
end)

-- Waiting: setting aside --------------------------------------------------------------

type SetAside = { pane: number, cwd: string?, note: string, waitingFor: string, label: string }

-- A pane that runs no omp session: Tern's own picker sets it aside as a pane record.
local function setAsideTarget(pane: number, info: PaneInfo): SetAside
	local record = waitingRecord(pane)
	return {
		pane = pane, cwd = info.cwd,
		note = if record and type(record.note) == "string" then record.note else "",
		waitingFor = if record and type(record.waiting_for) == "string" then record.waiting_for else "",
		label = clipped(if info.running then info.running.line else info.title, 60),
	}
end

-- Both end the flow when dismissed.
local function ask(cx: WindowCx, opts: PromptOptions, fn: (string, WindowCx) -> ())
	opts.icon = opts.icon or "clock"
	cx:prompt(opts):next(function(text, _err, cx2)
		if text ~= nil and cx2 then
			fn(trim(text), cx2)
		end
	end)
end

local function choose(cx: WindowCx, opts: PickOptions, fn: (any, WindowCx) -> ())
	opts.icon = opts.icon or "clock"
	cx:pick(opts):next(function(item, _err, cx2)
		if item ~= nil and cx2 then
			fn(item, cx2)
		end
	end)
end

-- `--in 2d` for a duration, `--at` for a date or a clock time; the CLI checks both.
local function timeArgs(text: string): { string }
	local compact = string.lower((string.gsub(text, "%s+", "")))
	local rest = string.gsub(compact, "%d+[smhdw]", "")
	if rest == "" and compact ~= "" then
		return { "--in", compact }
	end
	return { "--at=" .. text }
end

-- Runs the CLI command that sets the pane aside, then parks the pane if asked to.
local function commitSetAside(t: SetAside, args: { string }, park: boolean)
	waitingCli(args, function(data, err, cx)
		if not data then
			waitingFailed("set aside", err, cx)
		elseif cx then
			local open = dashboard(cx)
			if open then
				closePane(cx, open.pane)
			end
			if not park then
				return
			end
			afterRestore(cx, function(cx2)
				local ok, parkErr = cx2.layout:park(t.pane)
				if not ok then
					waitingFailed("park", parkErr, cx2)
				end
			end)
		end
	end)
end

-- The flow's last step: park the pane, or leave it where it is.
local function askPark(cx: WindowCx, fn: (boolean, WindowCx) -> ())
	choose(cx, { placeholder = "Park the pane?", items = {
		{ label = "Park it", detail = "off the screen until you open it", park = true },
		{ label = "Keep it open", park = false },
	} }, function(item, cx2)
		fn(item.park, cx2)
	end)
end

-- The last steps: my sentence, whether to park, then `add` and the park.
local function finishSetAside(cx: WindowCx, t: SetAside, kind: string, args: { string })
	local cwd = if t.cwd and string.sub(t.cwd, 1, 1) == "/" then t.cwd else nil
	if kind == "command" and not cwd then
		waitingFailed("set aside", "the pane's folder is unknown", cx)
		return
	end
	if cwd and (kind == "command" or kind == "release") then
		table.insert(args, "--cwd")
		table.insert(args, cwd)
	end
	ask(cx, { value = t.note, placeholder = "Why set it aside, what next?", action = "next" }, function(note, cx2)
		-- Free text as `--flag=value`: argparse takes a separate "-zsh" for an option.
		local full = { "add", "--pane", tostring(t.pane), "--kind", kind, "--by", "user", "--label=" .. t.label }
		for _, a in args do
			table.insert(full, a)
		end
		if note ~= "" then
			table.insert(full, "--note=" .. note)
		end
		askPark(cx2, function(park)
			commitSetAside(t, full, park)
		end)
	end)
end

local function setAsidePr(cx: WindowCx, t: SetAside, target: { string })
	choose(cx, { placeholder = "Until", items = {
		{ label = "Ready", detail = "checks green, bot reviewed the head, no open thread", until_ = "ready" },
		{ label = "Review on the head", detail = "a review bot reviewed the latest push", until_ = "review_on_head" },
		{ label = "Merged", until_ = "merged" },
		{ label = "Released", detail = "a release's tag contains the merge", until_ = "released" },
		{ label = "Any activity", detail = "someone else comments, reviews, pushes, merges or closes it", until_ = "activity" },
	} }, function(item, cx2)
		local args = { "--until", item.until_ }
		for _, a in target do
			table.insert(args, a)
		end
		finishSetAside(cx2, t, "pr", args)
	end)
end

-- The pane's repository as owner/repo; nil and the reason when `gh` can't tell.
local function paneRepo(t: SetAside, timeoutMs: number, done: (string?, string?, WindowCx?) -> ())
	if not t.cwd or string.sub(t.cwd, 1, 1) ~= "/" then -- `gh` would answer for the plugin's own folder
		done(nil, "the pane's folder is unknown", nil)
		return
	end
	local ok, err = pcall(tern.process.run, { "gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner" },
		{ cwd = t.cwd, timeout_ms = timeoutMs }, function(res: ProcessResult, cx: WindowCx?)
			local repo = trim(res.stdout)
			if res.status == 0 and repo ~= "" then
				done(repo, nil, cx)
			else
				local why = firstLine(res.stderr)
				done(nil, if why ~= "" then why else "gh repo view failed", cx)
			end
		end)
	if not ok then
		done(nil, tostring(err), nil)
	end
end

-- A bare number needs the pane's repository.
local function prTarget(text: string, t: SetAside, done: ({ string }?, string?, WindowCx?) -> ())
	local number = string.match(text, "^#?(%d+)$")
	if not number then
		done({ "--pr=" .. text }, nil, nil)
		return
	end
	paneRepo(t, 10000, function(repo, err, cx)
		if repo then
			done({ "--repo", repo, "--number", number }, nil, cx)
		else
			done(nil, tostring(err) .. "; give owner/repo#N", cx)
		end
	end)
end

local SET_ASIDE_FLOWS: { [string]: (WindowCx, SetAside) -> () } = {
	time = function(cx, t)
		ask(cx, { value = "1d", placeholder = "When? 2d, 18:00 or 2026-11-07 09:00", action = "next" }, function(text, cx2)
			finishSetAside(cx2, t, "time", timeArgs(text))
		end)
	end,
	job = function(cx, t)
		choose(cx, { placeholder = "When the job ends", items = {
			{ label = "Keep the pane", close = false },
			{ label = "Close the pane on success", detail = "exit 0 closes it, a failure lands on you", close = true },
		} }, function(item, cx2)
			finishSetAside(cx2, t, "job", if item.close then { "--close-on-success" } else {})
		end)
	end,
	pr = function(cx, t)
		ask(cx, { placeholder = "PR URL, owner/repo#N or number", action = "next" }, function(text, cx2)
			if text == "" then
				waitingFailed("set aside", "give a PR URL, owner/repo#N or number", cx2)
				return
			end
			prTarget(text, t, function(target, err, cx3)
				local next_ = cx3 or cx2
				if target then
					setAsidePr(next_, t, target)
				else
					waitingFailed("find the repository", err, next_)
				end
			end)
		end)
	end,
	release = function(cx, t)
		-- The picker has closed by now: a slow `gh` must not hold the next prompt for long.
		paneRepo(t, 3000, function(repo, _err, cx2)
			ask(cx2 or cx, { value = repo, placeholder = "owner/repo", action = "next" }, function(text, cx3)
				if string.match(text, "^[%w%._%-]+/[%w%._%-]+$") then
					finishSetAside(cx3, t, "release", { "--repo=" .. text })
				else
					waitingFailed("wait for a release", "give owner/repo", cx3)
				end
			end)
		end)
	end,
	check = function(cx, t) -- a literal command: no agent writes it here
		ask(cx, { placeholder = "Command line; done when it exits 0", action = "next" }, function(argv, cx2)
			ask(cx2, { value = "15m", placeholder = "Check every", action = "next" }, function(interval, cx3)
				finishSetAside(cx3, t, "command", { "--argv=" .. argv, "--interval=" .. interval })
			end)
		end)
	end,
	-- The pane's one free-text status: `what` sets it (or updates the one it has); its text says it all.
	["for"] = function(cx, t)
		ask(cx, { value = t.waitingFor, placeholder = "What are you waiting for?", action = "next" }, function(what, cx2)
			if what == "" then
				waitingFailed("set aside", "say what you wait for", cx2)
			else
				askPark(cx2, function(park)
					commitSetAside(t, { "what", "--pane", tostring(t.pane), "--text=" .. what }, park)
				end)
			end
		end)
	end,
}

-- The shared menu's items for a pane (`waitingMenu`, published by the host from
-- waiting_menu.json), in the file's order; nil and the host's error without
-- them, nil alone while the host is still loading it.
local function paneItems(): ({ PickItem }?, string?)
	local menu = tern.kv.get("waitingMenu")
	if type(menu) ~= "table" or type(menu.items) ~= "table" then
		return nil, if type(menu) == "table" and type(menu.error) == "string" then menu.error else nil
	end
	local items: { PickItem } = {}
	for _, it in menu.items do -- kv tables are read-only proxies: plain loops, no table library
		local forPane = false
		for _, target in (if type(it) == "table" and type(it.targets) == "table" then it.targets else {}) do
			forPane = forPane or target == "pane"
		end
		if forPane and SET_ASIDE_FLOWS[it.id] then
			local detail = if type(it.pane_detail) == "string" then it.pane_detail else tostring(it.detail)
			local command = if type(it.command) == "string" and it.command ~= "" then it.command .. " · " else ""
			table.insert(items, { label = tostring(it.label), detail = command .. detail, flow = it.id })
		elseif forPane then
			tern.log.warn("codexbar: waiting_menu.json item has no Tern flow", tostring(it.id))
		end
	end
	return items, nil
end

-- An omp session's menu is the extension's, drawn in its composer: Tern only
-- asks for it, and brings the pane forward for the menu to be seen.
local function requestMenu(pane: number)
	waitingCli({ "menu-request", "--pane", tostring(pane) }, function(data, err, cx)
		if not cx then
			return
		end
		if not data then
			waitingFailed("open the wait menu", err, cx)
		elseif data.menu ~= true then
			-- The reason says what to do, so it leads: a toast's second line is clipped.
			cx:toast("info", "No wait menu: " .. (if type(data.reason) == "string" then data.reason else "the omp extension did not answer"))
		elseif cx.session:focused() ~= pane then
			local open = dashboard(cx)
			if open then
				closePane(cx, open.pane)
			end
			afterRestore(cx, function(cx2)
				cx2.layout:focus(pane)
			end)
		end
	end)
end

local function setAside(cx: WindowCx)
	local pane, info = focusedTarget(cx)
	if not pane or not info then
		cx:toast("info", "CodexBar AgentView: no pane to set aside")
		return
	end
	local extension = paneExtension(pane)
	if extension and extension.menu == true then
		requestMenu(pane)
		return
	elseif extension or paneSession(pane) then
		-- Decided here, as the view knows: the CLI would only say the same.
		cx:toast("info", "No wait menu: " .. (if extension then "this session runs an older waitingfor extension: /restart it" else "no omp extension in this pane"))
		return
	end
	local items, err = paneItems()
	if not items and not err then
		cx:toast("info", "CodexBar AgentView: the wait menu is still loading")
		return
	elseif not items or #items == 0 then
		cx:toast("error", "CodexBar AgentView: no wait menu", err or "waiting_menu.json lists nothing for panes")
		return
	end
	local t = setAsideTarget(pane, info)
	choose(cx, { items = items, placeholder = "What does this wait for?", action = "next" }, function(item, cx2)
		SET_ASIDE_FLOWS[item.flow](cx2, t)
	end)
end

-- Not Ctrl+Shift+W: Tern's preset closes the pane with it, and a preset's chord stays the preset's.
tern.command({ id = "set_aside", title = "CodexBar AgentView: Set aside…", icon = "clock", keys = { "ctrl+alt+b" }, run = setAside })

tern.command({ id = "install_omp_extension", title = "CodexBar AgentView: Install omp extension", icon = "puzzle", run = function(_cx)
	python("install_integration.py", { "--omp-extension" }, function(res, cx)
		if not cx then
			return
		end
		if res.status == 0 then
			cx:toast("success", "omp extension installed", "omp sessions load it when they start")
		else
			cx:toast("error", "CodexBar AgentView: installing the omp extension failed", firstLine(res.stderr))
		end
	end)
end })

-- The dashboard is a temporary sidebar: focus landing anywhere else (another
-- pane, another tab, a session it switched to) closes it.
tern.on("focus", function(ev, cx)
	remember(cx, ev.pane)
	local info = ev.pane and paneInfo(cx, ev.pane)
	if not info or info.block == KIND then
		return
	end
	for _, p in cx.session:panes() do
		if p.block == KIND then
			closePane(cx, p.pane)
		end
	end
	-- The first focus after a wait fired brings the session card, once: `poll`
	-- forgets the pane when the host publishes it as seen.
	local entry = waitingEntry(ev.pane)
	if entry and entry.unseen and not seenAsked[ev.pane] then
		seenAsked[ev.pane] = true
		afterRestore(cx, function(cx2)
			openCard(cx2, ev.pane)
		end)
	end
end)

tern.on("pane_closed", function(ev, _cx)
	local o = opened
	if o and ev.pane == o.pane then
		o.placing = false
		o.closed = true
		tern.timer(0, function(cx2)
			if cx2 then
				restoreLayout(cx2)
			end
		end)
	end
	local entry = waitingEntry(ev.pane)
	if entry and entry.record then
		waitingSend("pane event", { "event", "--type", "pane_closed", "--pane", tostring(ev.pane) })
	end
end)

-- Redraws the status line when the host publishes a new summary.
local summarySig = ""
local initialSettingsRequest = tern.kv.get("settingsRequest")
local lastSettingsRequest = if type(initialSettingsRequest) == "table" then initialSettingsRequest.seq else nil
local initialWaitingRequest = tern.kv.get("waitingRequest")
local lastWaitingRequest = if type(initialWaitingRequest) == "table" then initialWaitingRequest.seq else nil
-- A request no window took up within this long is dropped: the target may come
-- to this window much later, and opening it then would steal focus.
local REQUEST_TTL_S = 15

-- Dashboard buttons that need a window: like picking from a popup, they close
-- the dashboard, and what they do waits for its layout to come back. The list
-- spans windows, so the window holding the target answers too, and marks it
-- taken in `waitingRequestTaken`. "Waiting for…" asks over the dashboard
-- instead, which shows the new text once the CLI answers.
local function waitingRequest(cx: WindowCx)
	local request = tern.kv.get("waitingRequest")
	if type(request) ~= "table" or type(request.pane) ~= "number" or request.seq == lastWaitingRequest then
		return
	end
	local seq = request.seq
	if type(request.at) == "number" and os.time() - request.at > REQUEST_TTL_S then
		lastWaitingRequest = seq
		return
	end
	local info = paneInfo(cx, request.pane)
	local ours = info ~= nil and info.block == KIND
	local held = request.target ~= nil and paneInfo(cx, request.target) ~= nil
	if not ours and not held then
		return
	end
	lastWaitingRequest = seq
	if held then
		tern.kv.set("waitingRequestTaken", seq)
	end
	if request.op == "what" then
		local args: { string } = {} -- kv tables are read-only proxies
		for _, a in (if type(request.args) == "table" then request.args else {}) do
			table.insert(args, tostring(a))
		end
		ask(cx, { value = tostring(request.text or ""), placeholder = "What are you waiting for? Empty clears it", action = "save" }, function(text)
			table.insert(args, "--text=" .. text)
			waitingCli(args, function(data, err, cx2)
				if not data then
					waitingFailed("set what you wait for", err, cx2)
				end
			end)
		end)
		return
	end
	if ours then
		closePane(cx, request.pane)
	end
	afterRestore(cx, function(cx2)
		if request.op == "launch" then
			-- As History's Launch: an interactive shell loads the user's PATH and stays when the agent exits.
			local shell = shellQuote(tern.getenv("SHELL") or "/bin/sh")
			cx2.layout:new_tab({
				cwd = request.cwd,
				command = shell .. " -ic " .. shellQuote(request.command .. "; exec " .. shell .. " -i"),
			})
			return
		end
		local target = tonumber(request.target)
		local shown = if target then paneInfo(cx2, target) else nil
		if not target or not shown then
			-- The window holding it opens it; the host already said when it closed.
			tern.timer(2000, function(cx3)
				if cx3 and tern.kv.get("waitingRequestTaken") ~= seq then
					cx3:toast("info", "CodexBar AgentView: no window shows that pane")
				end
			end)
			return
		end
		if shown.parked then
			-- A session coming back gets a tab of its own, not a share of the current one.
			local ok, err = cx2.layout:unpark(target)
			if ok then
				local tab, moveErr = cx2.layout:move_to_new_tab(target)
				ok, err = tab ~= nil, moveErr
			end
			if not ok then
				waitingFailed("open", err, cx2)
				return
			end
		end
		cx2.layout:focus(target)
		if request.card then
			openCard(cx2, target)
		end
	end)
end

-- omp's status row links `codexbar-agentview://card/<session id>` inside Tern;
-- the row is drawn in that session's own pane, the one clicked. The host half
-- opens `codexbar-agentview://request` after it publishes `waitingRequest`: the
-- one way it can reach this half without waiting for the poll.
tern.route.link(function(link: LinkRequest, cx: WindowCx): RouteDecision?
	if link.url == "codexbar-agentview://request" then
		local ok, err = pcall(waitingRequest, cx)
		if not ok then
			tern.log.error("codexbar:", err)
		end
		return { handled = true }
	end
	local id = string.match(link.url, "^codexbar%-agentview://card/([^/?#]+)/?$")
	if not id then
		return nil
	end
	toggleCardOn(cx, link.pane, id)
	return { handled = true }
end)

local function poll(cx: WindowCx?)
	local s = tern.kv.get("summary")
	local sig = if s ~= nil then tern.json.encode(s) else ""
	if sig ~= summarySig then
		summarySig = sig
		tern.chrome.refresh()
		for pane in seenAsked do
			local entry = waitingEntry(pane)
			if not (entry and entry.unseen) then
				seenAsked[pane] = nil
			end
		end
	end
	if cx then
		restoreLayout(cx)
		local request = tern.kv.get("settingsRequest")
		if type(request) == "table" and type(request.pane) == "number" and request.seq ~= lastSettingsRequest then
			local info = paneInfo(cx, request.pane)
			if info and info.block == KIND then
				lastSettingsRequest = request.seq
				openSettings(cx)
			end
		end
		for _, step in { waitingRequest, dropOrphanCards, lead, refreshCards } do
			local ok, err = pcall(step, cx)
			if not ok then
				tern.log.error("codexbar:", err)
			end
		end
	end
	-- What `codexbar-agentview://request` misses comes through here: a request from another window's dashboard.
	tern.timer(if cx and dashboard(cx) then 250 else 1000, poll)
end

-- A timer rather than `window_start`: that hook fires once per window, not again after a plugin reload.
tern.timer(0, function(cx)
	if cx then
		remember(cx, cx.session:focused())
	end
	poll(cx)
end)
