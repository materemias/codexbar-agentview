-- Window half of the CodexBar AgentView plugin: status-line segments, palette commands,
-- and the pane you came from, which the dashboard marks with a ring.
--
-- The host half publishes `summary` (status-line data), `open` (live blocks)
-- and `settingsRequest` (dashboard settings button) through `tern.kv`; this
-- half answers with `goto` (switch a dashboard tab) and `focus` (the last
-- focused pane that is not a CodexBar AgentView block).
--
-- Pane ids: after a daemon upgrade in place, ids reach 2^44 and beyond, and the
-- window API cuts them to their low 44 bits. Panes are matched on those bits,
-- and actions on a dashboard go through the `tern` CLI, which takes full ids.

local KIND = "codexbar-agentview.dashboard"
local SETTINGS_KIND = "codexbar-agentview.settings"
local PANE_TAG = 2 ^ 44
local HOME = tern.getenv("HOME") or ""
local TERN = if HOME ~= "" and tern.fs.exists(HOME .. "/.local/bin/tern") then HOME .. "/.local/bin/tern" else "tern"

local function cli(args: { string }, done: ((ProcessResult, WindowCx?) -> ())?, timeout: number?): boolean
	local argv = { TERN }
	for _, a in args do
		table.insert(argv, a)
	end
	local ok, err = pcall(tern.process.run, argv, { timeout_ms = timeout or 5000 }, function(res, cx)
		if res.status ~= 0 then
			tern.log.warn("codexbar: tern " .. table.concat(args, " ") .. " failed", string.match(res.stderr, "[^\n]+") or res.status)
		end
		if done then
			done(res, cx)
		end
	end)
	if not ok then
		tern.log.warn("codexbar: cannot run tern", err)
	end
	return ok
end

local function paneInfo(cx: WindowCx, pane: number): PaneInfo?
	for _, p in cx.session:panes() do
		if p.pane % PANE_TAG == pane % PANE_TAG then
			return p
		end
	end
	return nil
end

-- The dashboard this window opened, with the layout from before it opened.
type Popup = {
	pane: number, tab: number?, full: number?, snap: LayoutSnapshot,
	deadline: number, tries: number, restoring: boolean, placing: boolean,
}
local opened: Popup? = nil

-- The full pane id of a CodexBar AgentView block, when the host published it; the window
-- API only sees the low 44 bits and can't act on panes above that.
local function fullId(pane: number): number?
	if opened and opened.full and opened.pane % PANE_TAG == pane % PANE_TAG then
		return opened.full
	end
	local open = tern.kv.get("open")
	for _, e in (if type(open) == "table" then open else {}) do
		local id = tonumber(e.pane)
		if id and id % PANE_TAG == pane % PANE_TAG then
			return id
		end
	end
	return nil
end

local function closePane(cx: WindowCx, pane: number)
	if opened and opened.pane % PANE_TAG == pane % PANE_TAG then
		opened.placing = false
	end
	local full = fullId(pane)
	if full then
		cli({ "close", string.format("%d", full) })
	else
		cx.layout:close(pane)
	end
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

-- Resolve the host-published full id, not the original focused or shown tab.
-- Split ratios give each tiled pane's normalized left/top bounds.
local function placement(stdout: string, full: number, tab: number): Placement?
	local function validId(id: any): boolean
		return type(id) == "number" and id > 0 and id < 2 ^ 53 and id % 1 == 0
	end
	assert(validId(full) and validId(tab), "Invalid CodexBar AgentView pane or tab id")
	local data = tern.json.decode(stdout)
	assert(type(data) == "table" and type(data.sessions) == "table", "Invalid Tern session list")
	local found = nil
	for _, s in data.sessions do
		assert(type(s) == "table" and type(s.tabs) == "table", "Invalid Tern tabs")
		for _, t in s.tabs do
			assert(type(t) == "table" and validId(t.id), "Invalid Tern tab id")
			assert(type(t.blocks) == "table", "Invalid Tern block list")
			for _, b in t.blocks do
				assert(type(b) == "table" and validId(b.id), "Invalid Tern block id")
				if b.id == full then
					assert(not found, "Ambiguous CodexBar AgentView dashboard id")
					found = t
				end
			end
		end
	end
	if not found then
		return nil
	end
	assert(found.id % PANE_TAG == tab % PANE_TAG, "CodexBar AgentView dashboard belongs to a different tab")
	local blocks, seen = {}, {}
	for _, b in found.blocks do
		assert(not blocks[b.id], "Duplicate Tern block id")
		blocks[b.id] = true
	end
	local popupX, target = nil, nil
	local targetX, targetY = -1, -1
	local epsilon = 1e-6
	local tiles = 0
	local function walk(n: any, x: number, y: number, w: number, h: number)
		assert(type(n) == "table", "Invalid Tern split tree")
		if n.Leaf ~= nil then
			assert(n.Split == nil and validId(n.Leaf) and blocks[n.Leaf] and not seen[n.Leaf], "Invalid Tern split leaf")
			seen[n.Leaf] = true
			tiles += 1
			if n.Leaf == full then
				popupX = x
			elseif x > targetX + epsilon or (math.abs(x - targetX) <= epsilon and y > targetY) then
				target, targetX, targetY = n.Leaf, x, y
			end
		else
			local split = n.Split
			assert(type(split) == "table" and (split.axis == "Row" or split.axis == "Column"), "Invalid Tern split axis")
			local ratio = split.ratio
			assert(type(ratio) == "number" and ratio >= 0 and ratio <= 1, "Invalid Tern split ratio")
			if split.axis == "Row" then
				walk(split.a, x, y, w * ratio, h)
				walk(split.b, x + w * ratio, y, w * (1 - ratio), h)
			else
				walk(split.a, x, y, w, h * ratio)
				walk(split.b, x, y + h * ratio, w, h * (1 - ratio))
			end
		end
	end
	walk(found.splits, 0, 0, 1, 1)
	assert(popupX ~= nil, "CodexBar AgentView dashboard is not tiled")
	return { target = target, rightmost = popupX >= targetX - epsilon, tiles = tiles }
end

local function placementActive(cx: WindowCx, o: Popup): boolean
	if opened ~= o or not o.placing or o.restoring then
		return false
	end
	local focused = cx.session:focused()
	local info = paneInfo(cx, o.pane)
	if not focused or focused % PANE_TAG ~= o.pane % PANE_TAG
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

local function placeDashboard(cx: WindowCx, o: Popup)
	if not placementActive(cx, o) then
		return
	end
	local function retry(current: WindowCx)
		local remaining = o.deadline - tern.now()
		if remaining <= 0 then
			placementFailed(current, o, "Timed out waiting for CodexBar AgentView dashboard registration")
			return
		end
		tern.timer(math.min(50, remaining), function(cx2)
			if cx2 then
				placeDashboard(cx2, o)
			end
		end)
	end
	local remaining = o.deadline - tern.now()
	if remaining <= 0 then
		placementFailed(cx, o, "Timed out waiting for CodexBar AgentView dashboard registration")
		return
	end
	-- Wait for the daemon-published full id before querying its layout.
	local full = fullId(o.pane)
	if not full or not o.tab then
		retry(cx)
		return
	end
	o.full = full
	local ok = cli({ "ls", "--json" }, function(res, cx2)
		if not cx2 or not placementActive(cx2, o) then
			return
		end
		if res.status ~= 0 then
			local err = if tern.now() >= o.deadline then "Timed out waiting for CodexBar AgentView dashboard registration" else "Could not read the Tern layout"
			placementFailed(cx2, o, err)
			return
		end
		local parsed, result = pcall(placement, res.stdout, full, o.tab)
		if not parsed then
			placementFailed(cx2, o, tostring(result))
			return
		end
		if not result then
			retry(cx2)
			return
		end
		local targets = {}
		local movesLeft = result.tiles - 1
		local function position(current: WindowCx, layout: Placement)
			if not placementActive(current, o) then
				return
			end
			local function equalizeAndVerify(nextCx: WindowCx)
				if not placementActive(nextCx, o) then
					return
				end
				-- Equalization can change which stacked row has the rightmost leaf.
				nextCx:action("equalize")
				local checking = cli({ "ls", "--json" }, function(final, cx3)
					if not cx3 or not placementActive(cx3, o) then
						return
					end
					if final.status ~= 0 then
						placementFailed(cx3, o, "Could not verify the Tern layout")
						return
					end
					local valid, placed = pcall(placement, final.stdout, full, o.tab)
					if not valid then
						placementFailed(cx3, o, tostring(placed))
					elseif not placed then
						placementFailed(cx3, o, "CodexBar AgentView dashboard disappeared from its tab")
					elseif placed.rightmost then
						o.placing = false
					else
						position(cx3, placed)
					end
				end)
				if not checking then
					placementFailed(nextCx, o, "Could not start the Tern layout verification")
				end
			end
			if layout.rightmost then
				equalizeAndVerify(current)
				return
			end
			local target = layout.target :: number
			-- Removing and reinserting only this leaf preserves the underlying tree.
			-- A repeated target cannot progress; new targets are bounded by its size.
			if targets[target] or movesLeft <= 0 then
				placementFailed(current, o, "CodexBar AgentView dashboard placement did not progress")
				return
			end
			targets[target] = true
			movesLeft -= 1
			local moved = cli({ "move", string.format("%d", full), "right-of", string.format("%d", target) }, function(movedResult, cx3)
				if not cx3 or not placementActive(cx3, o) then
					return
				end
				if movedResult.status ~= 0 then
					placementFailed(cx3, o, "Could not move the CodexBar AgentView dashboard")
					return
				end
				equalizeAndVerify(cx3)
			end)
			if not moved then
				placementFailed(current, o, "Could not start the Tern move command")
			end
		end
		position(cx2, result)
	end, math.max(1, math.ceil(remaining)))
	if not ok then
		placementFailed(cx, o, "Could not start the Tern layout query")
	end
end

-- Opens the dashboard on `tab`; on the shown, focused tab it closes it, like a popup.
local function toggle(cx: WindowCx, tab: string)
	local info, shown = dashboard(cx)
	if not info then
		-- Capture the original layout before creating the dashboard.
		local snap = cx.layout:snapshot()
		local pane = cx:new_block(KIND, { tab }, "beside", { focus = true })
		if pane then
			local o: Popup = {
				pane = pane, snap = snap, deadline = tern.now() + 3000,
				tries = 0, restoring = false, placing = true,
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
	local full = fullId(info.pane)
	tern.kv.set("goto", { pane = full or info.pane, tab = tab, seq = tern.now() })
	if not info.focused then
		if full then
			cli({ "focus", string.format("%d", full) })
		else
			cx.layout:focus(info.pane)
		end
	end
end

-- Settings are a persistent tab, not a dashboard sidebar.
local function openSettings(cx: WindowCx)
	for _, p in cx.session:panes() do
		if p.block == SETTINGS_KIND then
			local full = fullId(p.pane)
			if full then
				cli({ "focus", string.format("%d", full) })
			else
				cx.layout:focus(p.pane)
			end
			return
		end
	end
	cx:new_block(SETTINGS_KIND, nil, "tab", { focus = true })
end

tern.command({ id = "agents", title = "CodexBar AgentView: Agents", icon = "users", keys = { "ctrl+alt+a" }, run = function(cx)
	toggle(cx, "agents")
end })
tern.command({ id = "usage", title = "CodexBar AgentView: Usage", icon = "gauge", keys = { "ctrl+alt+u" }, run = function(cx)
	toggle(cx, "usage")
end })
tern.command({ id = "history", title = "CodexBar AgentView: History", icon = "history", run = function(cx)
	toggle(cx, "history")
end })
tern.command({ id = "settings", title = "CodexBar AgentView: Settings", icon = "sliders", run = function(cx)
	openSettings(cx)
end })

tern.chrome.status(function(_pane)
	local s = tern.kv.get("summary")
	if type(s) ~= "table" then
		return nil
	end
	local segments = {}
	for _, u in (if type(s.usage) == "table" then s.usage else {}) do
		table.insert(segments, { text = u.text, icon = u.icon, tone = u.tone, command = "plugin.codexbar-agentview.usage" })
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
	if info and info.block ~= KIND and info.block ~= SETTINGS_KIND and tern.kv.get("focus") ~= pane then
		tern.kv.set("focus", pane)
	end
end

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
end)

-- However the dashboard closes (toggle, Esc, switching to a session, focus
-- leaving it, closing the pane), the layout returns to how it was before it
-- opened. `pane_closed` fires before the tree drops the pane, `tern close`
-- from the CLI sends none, and a pane can leave `session:panes()` while its
-- tab tree still holds it. So once the pane is gone the restore waits for
-- the trees too, and is retried every 100 ms for 2 s; a refusal that lasts
-- means other blocks really changed, and the layout stays as it is.
local function inTrees(cx: WindowCx, pane: number): boolean
	local function walk(n: any): boolean
		if type(n) ~= "table" then
			return false
		end
		local p = n.pane
		if type(p) == "number" and p % PANE_TAG == pane % PANE_TAG then
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
	local ok, err = false, "the dashboard is still in the layout"
	if not inTrees(cx, o.pane) then
		ok, err = cx.layout:restore(o.snap)
	end
	o.tries += 1
	if ok or o.tries >= 20 then
		opened = nil
		if not ok then
			tern.log.info("codexbar: layout not restored", err)
		end
		return
	end
	tern.timer(100, function(cx2)
		if cx2 then
			attemptRestore(cx2, o)
		end
	end)
end

local function restoreLayout(cx: WindowCx)
	local o = opened
	if not o or o.restoring or paneInfo(cx, o.pane) then
		return
	end
	if o.placing and o.tab == nil and tern.now() < o.deadline then
		return
	end
	o.restoring = true
	o.placing = false
	attemptRestore(cx, o)
end

tern.on("pane_closed", function(ev, _cx)
	local o = opened
	if o and ev.pane % PANE_TAG == o.pane % PANE_TAG then
		o.placing = false
		tern.timer(50, function(cx)
			if cx then
				restoreLayout(cx)
			end
		end)
	end
end)

-- Redraws the status line when the host publishes a new summary.
local summarySig = ""
local initialSettingsRequest = tern.kv.get("settingsRequest")
local lastSettingsRequest = if type(initialSettingsRequest) == "table" then initialSettingsRequest.seq else nil
local function poll(cx: WindowCx?)
	local s = tern.kv.get("summary")
	local sig = if s ~= nil then tern.json.encode(s) else ""
	if sig ~= summarySig then
		summarySig = sig
		tern.chrome.refresh()
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
	end
	tern.timer(1000, poll)
end

-- A timer rather than `window_start`: that hook fires once per window, not again after a plugin reload.
tern.timer(0, function(cx)
	if cx then
		remember(cx, cx.session:focused())
	end
	poll(cx)
end)
