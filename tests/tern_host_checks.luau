-- Checks of tern/host.luau, appended to it by tests/test_tern_luau.py after
-- tests/tern_fake.luau: they see its top-level locals. Inside a function, as
-- the main chunk is at Luau's 200-local limit.

do (function()
	local function fresh(daemon: string?, skipped: string?)
		fake.reset()
		store.daemonId, store.daemonKnown, store.waitingSkipped = daemon, true, skipped
		store.waiting, store.waitingTicking, store.waitingAgain, store.waitingRecheck = nil, false, false, false
		store.paneStates, store.panesListed, store.waitingMemo = {}, false, nil
		store.jobEventAt, store.paneGoneAt, store.jobPending = {}, {}, {}
		store.scanning, store.scanRetryAt, store.waitingRaw = false, 0, ""
	end

	local function view(fields: any): any
		return { sessions = fields.sessions or {}, panes = fields.panes or {}, counts = {}, health = {} }
	end

	local function jobRow(pane: number, daemon: string?): any
		return { pane = pane, daemon = daemon, waits = { { kind = "job", state = "waiting", condition = { pane = pane } } } }
	end

	local function fakeBlock(): any
		local cx = { pane = 99, toasts = {} :: { any } }
		function cx.toast(self, ...)
			table.insert(self.toasts, { ... })
		end
		function cx.open() end
		function cx.render() end
		return { cx = cx, launched = {}, closed = false }
	end

	local function eventRuns(kind: string): { any }
		local out = {}
		for _, r in fake.runsOf("codexbar_waiting.py") do
			if fake.has(r.argv, "event") and fake.has(r.argv, kind) then
				table.insert(out, r)
			end
		end
		return out
	end

	check("ours: a row is this daemon's by its stamp, else while this daemon ticks", function()
		fresh("d1")
		assert(ours({ daemon = "d1" }), "stamped with our id")
		assert(not ours({ daemon = "d2" }), "stamped with another daemon's id")
		assert(ours({}), "unstamped while we tick")
		assert(not ours({ daemon = "d1" }, { daemon = "d2" }), "the wait's stamp wins over the row's")
		fresh("d1", "another daemon ticks")
		assert(not ours({}), "unstamped while another daemon ticks")
		assert(ours({ daemon = "d1" }), "stamped ours while another daemon ticks")
		fresh(nil)
		store.daemonKnown = false
		assert(not ours({ daemon = "d1" }), "a stamp counts as nobody's until the id is known")
	end)

	check("jobPanes: only this daemon's job waits", function()
		fresh("d1")
		local panes = jobPanes(view({ panes = { jobRow(3, "d2"), jobRow(4, "d1"), jobRow(5) } }))
		assert(not panes[3] and panes[4] and panes[5], "panes " .. tern.json.encode(panes))
	end)

	check("waitingPanes: leaves out another daemon's rows", function()
		fresh("d1")
		store.waiting = view({
			panes = { { pane = 3, daemon = "d2", status = "x" }, { pane = 4, daemon = "d1", status = "y" }, { pane = 5, status = "z" } },
			sessions = { { pane = 6, daemon = "d2", live = true, session_id = "s6" } },
		})
		local panes = waitingPanes()
		assert(panes["3"] == nil and panes["6"] == nil, "a foreign row's pane listed")
		assert(panes["4"] ~= nil and panes["5"] ~= nil, "our rows missing")
	end)

	check("cardPane: a foreign row has no card pane", function()
		fresh("d1")
		assert(cardPane({ pane = 4, daemon = "d1" }, true) == 4, "our pane row")
		assert(cardPane({ pane = 4, daemon = "d2" }, true) == nil, "a foreign pane row")
		assert(cardPane({ pane = 4, daemon = "d2", session_id = "s", live = true }, true) == nil, "a foreign session row")
	end)

	check("waitingGroups: a foreign row's state is open", function()
		fresh("d1")
		store.panesListed = true
		store.paneStates = { [4] = "running" }
		local mine = { pane = 4, ball = "on_me", daemon = "d1" }
		local foreign = { pane = 4, ball = "on_me", daemon = "d2" }
		local gone = { pane = 8, ball = "on_me", daemon = "d1" }
		store.waiting = view({ panes = { mine, foreign, gone } })
		local states = {}
		for _, g in waitingGroups("") do
			for _, r in g.rows do
				states[r.daemon .. ":" .. r.pane] = g.states[r]
			end
		end
		assert(states["d1:4"] == "running", "ours: " .. tostring(states["d1:4"]))
		assert(states["d2:4"] == "open", "foreign: " .. tostring(states["d2:4"]))
		assert(states["d1:8"] == "closed", "ours gone: " .. tostring(states["d1:8"]))
	end)

	check("waitingOpen: a foreign row targets none of this daemon's panes", function()
		fresh("d1")
		fake.panes = { { pane = 4 } }
		local b = fakeBlock()
		waitingOpen(b, { pane = 4, daemon = "d2", session_id = "s", live = true })
		assert(fake.kv.waitingRequest == nil, "requested " .. tern.json.encode(fake.kv.waitingRequest))
		assert(#fake.runsOf("codexbar_focus.py") == 1, "a foreign live session is focused where it runs")
		waitingOpen(b, { pane = 4, daemon = "d2" })
		assert(fake.kv.waitingRequest == nil, "requested a foreign pane row")
		assert(#b.cx.toasts == 1, "a foreign pane row says why nothing opens")
		waitingOpen(b, { pane = 4, daemon = "d1" })
		assert(fake.kv.waitingRequest and fake.kv.waitingRequest.target == 4, "our pane row opens")
	end)

	check("job_finished: resent from the tick until the CLI takes it", function()
		fresh("d1")
		fake.panes = { { pane = 7 } }
		store.waiting = view({ panes = { jobRow(7, "d1") } })
		fake.handlers.command_finished({ pane = 7, status = 3 })
		local sent = eventRuns("job_finished")
		assert(#sent == 1, "first send: " .. #sent)
		assert(fake.has(sent[1].argv, "3") and fake.has(sent[1].argv, "d1"), "args " .. table.concat(sent[1].argv, " "))
		reconcilePanes()
		assert(#eventRuns("job_finished") == 1, "no resend while one is in flight")
		assert(fake.answer(sent[1], { status = 1, stderr = "boom" }) == nil)
		reconcilePanes()
		sent = eventRuns("job_finished")
		assert(#sent == 2, "resend after a failure: " .. #sent)
		assert(fake.has(sent[2].argv, "3"), "resend keeps the status")
		assert(fake.answer(sent[2], { stdout = fake.json({ ok = true }) }) == nil)
		reconcilePanes()
		assert(#eventRuns("job_finished") == 2, "no resend after success")
	end)

	check("job_finished: a failed send is dropped once no wait watches the pane", function()
		fresh("d1")
		fake.panes = { { pane = 7 } }
		store.waiting = view({ panes = { jobRow(7, "d1") } })
		fake.handlers.command_finished({ pane = 7, status = 0 })
		assert(fake.answer(eventRuns("job_finished")[1], { status = 1 }) == nil)
		store.waiting = view({})
		reconcilePanes()
		store.waiting = view({ panes = { jobRow(7, "d1") } })
		reconcilePanes()
		assert(#eventRuns("job_finished") == 1, "resent after the wait was gone")
	end)

	check("measuredAt: the oldest of the enabled providers' newest readings", function()
		fresh("d1")
		fake.kv.settings = { providers = { "claude", "codex" } }
		local snap = { providers = {
			{ id = "claude", cachedAt = "2026-01-01T00:00:10Z" },
			{ id = "claude", cachedAt = "2026-01-01T00:00:30Z" },
			{ id = "codex", updatedAt = "2026-01-01T00:00:20Z" },
			{ id = "zai", cachedAt = "2026-01-01T00:05:00Z" },
		} }
		assert(measuredAt(snap, true) == epoch("2026-01-01T00:00:20Z"), "got " .. measuredAt(snap, true))
		assert(measuredAt(snap) == epoch("2026-01-01T00:05:00Z"), "the newest reading of any: " .. measuredAt(snap))
		table.remove(snap.providers, 3)
		assert(measuredAt(snap, true) == 0, "a provider without a reading counts as 0")
	end)

	check("scanAgents: no history limit, and a minute's wait after a failure", function()
		fresh("d1")
		scanAgents()
		local runs = fake.runsOf("codexbar_agents.py")
		assert(#runs == 1 and not fake.has(runs[1].argv, "--history-limit"), "argv " .. table.concat(runs[1].argv, " "))
		assert(fake.answer(runs[1], { status = 1, stderr = "boom" }) == nil)
		scanAgents()
		assert(#fake.runsOf("codexbar_agents.py") == 1, "rescanned right after a failure")
	end)

	check("waitingTick: the next tick is due even when applying the answer fails", function()
		fresh("d1")
		store.waitingAt, store.waitingAtMono, store.waitingSig = 0, 0, ""
		fake.failKv.waitingView = true
		waitingTick()
		local runs = fake.runsOf("codexbar_waiting.py")
		assert(#runs == 1, "ticks " .. #runs)
		local err = fake.answer(runs[1], { stdout = fake.json({ view = view({}), actions = {}, next_tick_s = 45 }) })
		assert(err == nil, "the callback raised: " .. tostring(err))
		assert(store.waitingAt >= os.time() + 40 and store.waitingAtMono >= os.clock() + 40, "next tick not scheduled")
		assert(not store.waitingTicking, "still ticking")
	end)

	check("applyUsage: a failure without stderr still says something", function()
		fresh("d1")
		applyUsage({ status = 2, stdout = "", stderr = "", timed_out = false })
		assert(store.usageError == "exit 2", "got " .. tostring(store.usageError))
		applyUsage({ status = 0, stdout = "not json", stderr = "", timed_out = false })
		assert(store.usageError == "unreadable answer", "got " .. tostring(store.usageError))
		applyUsage({ status = 1, stdout = "", stderr = "boom\nmore", timed_out = false })
		assert(store.usageError == "boom", "got " .. tostring(store.usageError))
	end)

	check("prefs: the About page lists companions once the helper answers, read again on each open", function()
		fresh("d1")
		store.companionsLoading = false
		local s = prefs.new(fakeBlock().cx, "about")
		prefs.new(fakeBlock().cx, "about")
		local runs = fake.runsOf("codexbar_companions.py")
		assert(#runs == 1, "a second open while one runs starts no other: " .. #runs)
		local answer = { companions = {
			{ id = "tern-awui", name = "tern-awui", url = "https://example.com/awui#install", benefit = "On the phone.",
				installed = true, status = "Installed" },
			{ id = "omp-compact", name = "omp-compact", url = "https://example.com/compact#install", benefit = "Folds tools.",
				installed = false, status = "Not found" },
			{ id = "bad", name = "bad", url = "file:///etc/passwd", benefit = "", installed = false, status = "" },
		} }
		assert(fake.answer(runs[1], { stdout = fake.json(answer) }) == nil)
		local sections = settingsSections(s)
		local section = sections[#sections]
		assert(section.title == "Works well with" and #section.rows == 2, tern.json.encode(section))
		local awui, compact = section.rows[1], section.rows[2]
		assert(awui.hint == "Installed. On the phone." and awui.control.label == "Open GitHub", tern.json.encode(awui))
		assert(compact.hint == "Not found. Folds tools." and compact.control.label == "Install steps", tern.json.encode(compact))
		prefs.event(s, { ev = "action", act = "page", value = "about" })
		runs = fake.runsOf("codexbar_companions.py")
		assert(#runs == 2, "switching to About reads the status again")
		answer.companions[2].installed, answer.companions[2].status = true, "Installed"
		assert(fake.answer(runs[2], { stdout = fake.json(answer) }) == nil)
		sections = settingsSections(s)
		assert(#sections[#sections].rows == 2 and sections[#sections].rows[2].control.label == "Open GitHub", "rows updated in place")
		prefs.event(s, { ev = "action", act = "open_github", value = "companion.omp-compact" })
		local opened = fake.runsOf("https://example.com/compact#install")
		assert(#opened == 1 and opened[1].argv[1] == "xdg-open", "opens the install link")
	end)

	fake.runChecks()
end)() end
