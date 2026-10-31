-- Window half of the CodexBar AgentView plugin: status-line segments, palette commands,
-- and the pane you came from, which the dashboard marks with a ring.
--
-- The host half publishes `summary` (status-line data) and `settingsRequest`
-- (dashboard settings button) through `tern.kv`; this half answers with `goto`
-- (switch a dashboard tab) and `focus` (the last focused pane that is not a
-- CodexBar AgentView block).
--
-- `tern.kv` and `tern.process` belong to the machine each half runs on: a window
-- attached to a remote host runs this half on its own machine, beside another
-- daemon. Layout work therefore goes through the window API only.

local KIND = "codexbar-agentview.dashboard"
local SETTINGS_KIND = "codexbar-agentview.settings"

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
	deadline: number, tries: number, restoring: boolean, placing: boolean,
	equalized: boolean, moves: number, targets: { [number]: boolean },
}
local opened: Popup? = nil

local function closePane(cx: WindowCx, pane: number)
	if opened and opened.pane == pane then
		opened.placing = false
	end
	cx.layout:close(pane)
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
		-- Capture the original layout before creating the dashboard.
		local snap = cx.layout:snapshot()
		local pane = cx:new_block(KIND, { tab }, "beside", { focus = true })
		if pane then
			local o: Popup = {
				pane = pane, snap = snap, deadline = tern.now() + 3000,
				tries = 0, restoring = false, placing = true,
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
	if o and ev.pane == o.pane then
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
