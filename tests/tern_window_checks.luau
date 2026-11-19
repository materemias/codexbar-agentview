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

	check("runActions: a resume an unnamed omp blocks reserves again, then fails", function()
		fresh()
		fake.kv.waitingActions = tern.json.array({ { id = "r1", type = "resume", session = "s1", cwd = "/w", command = "omp --resume s1" } })
		local cx = fakeCx("w1", "local", {})
		local blocked = { status = 11, stderr = "codexbar_focus: a running omp in /w may be writing this session\n" }
		runActions(cx, true)
		local runs = fake.runsOf("codexbar_focus.py")
		assert(#runs == 1, "reserved " .. #runs .. " times")
		local err = fake.answer(runs[1], blocked)
		assert(err == nil, err)
		local entry = fake.kv.waitingResumes.r1
		assert(entry.retryAt and entry.blocked and not entry.result, "stored " .. tern.json.encode(entry))
		runActions(cx, true)
		assert(#fake.runsOf("codexbar_focus.py") == 1, "reserved again before its time")
		assert(#fake.runsOf("codexbar_waiting.py") == 0, "acked a blocked resume")
		-- Its time came, and the omp still says nothing after RESERVE_BLOCKED_S.
		fake.kv.waitingResumes = { r1 = { blocked = os.clock() - RESERVE_BLOCKED_S - 1, retryAt = os.clock() - 1 } }
		runActions(cx, true)
		runs = fake.runsOf("codexbar_focus.py")
		assert(#runs == 2, "reserved " .. #runs .. " times")
		err = fake.answer(runs[2], blocked)
		assert(err == nil, err)
		assert(fake.kv.waitingResumes.r1.result == "failed", "stored " .. tern.json.encode(fake.kv.waitingResumes.r1))
		runActions(cx, true)
		local acks = fake.runsOf("codexbar_waiting.py")
		assert(#acks == 1 and fake.has(acks[1].argv, "failed"), "acks: " .. #acks)
	end)

	fake.runChecks()
end)() end
