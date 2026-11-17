-- Checks of tern/window.luau, appended to it by tests/test_tern_luau.py after
-- tests/tern_fake.luau: they see its top-level locals. Inside a function, to
-- add no local to its main chunk.

do (function()
	fake.frozenKv = true

	-- A window on `kind` ("local" or "remote") showing `panes` in `tabs`.
	local function fakeCx(key: string, kind: string, panes: { any }, tabs: { any }?): any
		local cx = { toasts = {} :: { string }, colored = {} :: { any }, parkedPanes = {} :: { number } }
		function cx.window_key()
			return key
		end
		cx.session = {
			panes = function()
				return panes
			end,
			tabs = function()
				return tabs or {}
			end,
			sessions = function()
				return {}
			end,
		}
		cx.hosts = {
			current = function()
				return { id = if kind == "local" then 0 else 1, name = kind, kind = kind, current = true, sessions = {}, panes = {} }
			end,
		}
		cx.layout = {
			color_tab = function(_, id: number, color: string?)
				table.insert(cx.colored, { id = id, color = color })
			end,
			park = function(_, pane: number)
				table.insert(cx.parkedPanes, pane)
				return true
			end,
		}
		function cx.toast(_, _level: string, text: string)
			table.insert(cx.toasts, text)
		end
		return cx
	end

	local function fresh()
		fake.reset()
		table.clear(acted)
		table.clear(heldSince)
	end

	check("colorTabs: drops its key once it colors no tab", function()
		fresh()
		fake.kv["waitingTabs:w1"] = { 3 }
		local cx = fakeCx("w1", "local", {}, { { id = 3, color = "yellow" } })
		colorTabs(cx)
		assert(#cx.colored == 1 and cx.colored[1].id == 3 and cx.colored[1].color == nil, "tab 3 not cleared")
		assert(fake.kv["waitingTabs:w1"] == nil, "kept " .. tern.json.encode(fake.kv["waitingTabs:w1"]))
	end)

	check("runActions: a toast shows once, whichever window leads", function()
		fresh()
		fake.kv.waitingActions = tern.json.array({ { id = "t1", type = "toast", level = "info", text = "hi" } })
		local first = fakeCx("w1", "local", {})
		runActions(first, true)
		assert(#first.toasts == 1, "first leader showed " .. #first.toasts)
		table.clear(acted) -- the lead moves to another window, before the ack landed
		local second = fakeCx("w2", "local", {})
		runActions(second, true)
		assert(#second.toasts == 0, "the next leader showed it again")
		fake.kv.waitingActions = tern.json.array()
		runActions(second, true)
		assert(fake.kv.waitingToasts == nil, "kept " .. tern.json.encode(fake.kv.waitingToasts))
	end)

	check("lead: a window on a remote host takes up no pane action", function()
		fresh()
		fake.kv.waitingActions = tern.json.array({ { id = "p1", type = "park", pane = 5 } })
		local remote = fakeCx("w1", "remote", { { pane = 5, tab = 1, parked = false } })
		lead(remote)
		assert(#remote.parkedPanes == 0, "parked a remote pane 5")
		local here = fakeCx("w2", "local", { { pane = 5, tab = 1, parked = false } })
		lead(here)
		assert(#here.parkedPanes == 1, "the local window parks pane 5")
	end)

	check("publishPanes: a window on a remote host publishes none of its panes", function()
		fresh()
		publishPanes(fakeCx("w1", "remote", { { pane = 5, parked = true } }))
		local entry = fake.kv.waitingPanes and fake.kv.waitingPanes.w1
		assert(entry and #entry.panes == 0 and #entry.parked == 0, "published " .. tern.json.encode(entry))
		publishPanes(fakeCx("w2", "local", { { pane = 5, parked = true } }))
		entry = fake.kv.waitingPanes.w2
		assert(entry and entry.panes[1] == 5 and entry.parked[1] == 5, "local panes missing")
	end)

	check("lead: a window on a remote host neither leads nor colors tabs", function()
		fresh()
		fake.kv.summary = { waiting = { panes = { ["5"] = { on_me = true, outcome = "attention" } } } }
		local remote = fakeCx("w1", "remote", { { pane = 5, tab = 1 } }, { { id = 1 } })
		lead(remote)
		assert(fake.kv.waitingLeader == nil, "took the lease")
		assert(#remote.colored == 0, "colored a remote tab")
		local here = fakeCx("w2", "local", { { pane = 5, tab = 1 } }, { { id = 1 } })
		lead(here)
		assert(fake.kv.waitingLeader and fake.kv.waitingLeader.key == "w2", "the local window leads")
		assert(#here.colored == 1 and here.colored[1].color == "yellow", "the local tab is colored")
	end)

	fake.runChecks()
end)() end
